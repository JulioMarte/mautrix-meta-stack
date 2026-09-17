import importlib
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import requests
import yaml


class FakeResponse:
    def __init__(self, *, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.content = b"{}" if payload is not None else b""

    def raise_for_status(self):
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            response.url = "http://chatwoot.example.com/adversarial"
            raise requests.HTTPError(response=response)

    def json(self):
        return self._payload


class ConversationDeletionAdversarialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-adversarial-tests"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"
        os.environ["MAUTRIX_REGISTRATION_PATH"] = os.path.join(cls.tmp.name, "registration.yaml")
        with open(os.environ["MAUTRIX_REGISTRATION_PATH"], "w", encoding="utf-8") as fh:
            yaml.safe_dump({
                "id": "meta",
                "sender_localpart": "metabot",
                "namespaces": {
                    "users": [
                        {"regex": r"^@meta_[0-9]+:matrix\.example\.com$", "exclusive": True},
                    ],
                    "aliases": [],
                    "rooms": [],
                },
            }, fh, sort_keys=False)

        global final, legacy, lifecycle, hardening
        final = importlib.import_module("final_app")
        legacy = final.legacy
        lifecycle = importlib.import_module("conversation_lifecycle_v10")
        hardening = importlib.import_module("conversation_lifecycle_hardening_v11")
        lifecycle.ensure_schema()
        hardening.install()
        if legacy.bridge_bot_mxid() != "@metabot:matrix.example.com":
            raise AssertionError(f"unexpected bridge bot mxid: {legacy.bridge_bot_mxid()!r}")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        with legacy.db() as conn:
            conn.execute("DELETE FROM settings")
            conn.execute("DELETE FROM room_links")
            conn.execute("DELETE FROM processed_events")
            conn.execute("DELETE FROM verified_meta_portals")
            conn.execute("DELETE FROM conversation_deletions")
        legacy.set_setting("chatwoot_base_url", "http://chatwoot.example.com")
        legacy.set_setting("chatwoot_account_id", "1")
        legacy.set_setting("chatwoot_inbox_id", "2")
        legacy.set_setting("chatwoot_api_token", "token")

    def seed(self, room="!portal:matrix.example.com", conversation=77):
        with legacy.db() as conn:
            conn.execute(
                "INSERT INTO room_links(room_id, contact_id, source_id, conversation_id, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (room, 5, "source-5", conversation, int(time.time())),
            )
        lifecycle.remember_verified_portal(room)
        return room

    def trusted_leave(self):
        return {
            "timeline": {
                "events": [{
                    "type": "m.room.member",
                    "state_key": legacy.MATRIX_ADMIN_MXID,
                    "sender": "@metabot:matrix.example.com",
                    "content": {"membership": "leave"},
                }]
            }
        }

    @staticmethod
    def delete_payload(conversation=77):
        return {
            "event": "conversation_deleted",
            "conversation_id": conversation,
            "account": {"id": 1},
            "inbox": {"id": 2},
        }

    def test_target_switch_clears_old_destructive_state_and_hmac_credentials(self):
        room = self.seed()
        lifecycle._start_operation(77, room, "meta", "remote_confirmed")
        legacy.set_setting("chatwoot_api_inbox_signing_secret", "old-api-inbox-hmac")
        legacy.set_setting("chatwoot_webhook_signing_secret", "old-account-webhook-hmac")
        legacy.set_setting("api_inbox_callback_verified_at", "old")
        legacy.set_setting("webhook_registration_verified_at", "old")

        old_target = final._chatwoot_target()
        legacy.set_setting("chatwoot_inbox_id", "9")
        new_target = final._chatwoot_target()
        final._reset_chatwoot_target_state(old_target, new_target)

        self.assertIsNone(lifecycle._operation(77))
        self.assertEqual(legacy.get_setting("chatwoot_api_inbox_signing_secret"), "")
        self.assertEqual(legacy.get_setting("chatwoot_webhook_signing_secret"), "")
        self.assertEqual(legacy.get_setting("api_inbox_callback_verified_at"), "")
        self.assertEqual(legacy.get_setting("webhook_registration_verified_at"), "")

    def test_matrix_timeout_retry_reuses_exact_same_transaction_id(self):
        self.seed()
        attempted_urls = []

        def timeout_after_accept(url, **kwargs):
            attempted_urls.append(url)
            raise requests.Timeout("response lost after homeserver accepted request")

        with patch.object(legacy, "matrix_headers", return_value={"Authorization": "Bearer test"}), \
             patch.object(lifecycle.requests, "put", side_effect=timeout_after_accept):
            with self.assertRaises(requests.Timeout):
                lifecycle.process_chatwoot_delete(self.delete_payload())

        first = lifecycle._operation(77)
        self.assertEqual(first["state"], "failed_retryable")
        self.assertEqual(len(attempted_urls), 1)

        def idempotent_retry(url, **kwargs):
            attempted_urls.append(url)
            return FakeResponse(payload={"event_id": "$same-delete-event"})

        with patch.object(legacy, "matrix_headers", return_value={"Authorization": "Bearer test"}), \
             patch.object(lifecycle.requests, "put", side_effect=idempotent_retry):
            result = lifecycle.process_chatwoot_delete(self.delete_payload())

        self.assertTrue(result["remote_requested"])
        self.assertEqual(attempted_urls[0], attempted_urls[1])
        self.assertIn("/send/com.beeper.delete_chat/cwdel-", attempted_urls[0])

    def test_failed_chatwoot_origin_submit_then_bridge_confirmation_never_deletes_chatwoot(self):
        room = self.seed()
        lifecycle._start_operation(77, room, "chatwoot", "pending")
        lifecycle._update_operation(
            77,
            state="failed_retryable",
            error="Matrix timeout after accept",
            increment_attempts=True,
            next_retry_at=0,
        )
        with patch.object(lifecycle.requests, "delete") as remote_delete:
            result = lifecycle.process_matrix_leave(room, self.trusted_leave())
        remote_delete.assert_not_called()
        self.assertEqual(result["origin"], "chatwoot")
        self.assertEqual(lifecycle._operation(77)["state"], "completed")
        self.assertIsNone(lifecycle._link_by_room(room))

    def test_meta_delete_timeout_callback_is_suppressed_then_404_retry_completes(self):
        room = self.seed()
        with patch.object(lifecycle.requests, "delete", return_value=FakeResponse(status=503)):
            with self.assertRaises(requests.HTTPError):
                lifecycle.process_matrix_leave(room, self.trusted_leave())

        failed = lifecycle._operation(77)
        self.assertEqual(failed["origin"], "meta")
        self.assertEqual(failed["state"], "failed_retryable")
        self.assertIsNotNone(lifecycle._link_by_room(room))

        callback = lifecycle.process_chatwoot_delete(self.delete_payload())
        self.assertEqual(callback["reason"], "meta_delete_loop_suppressed")

        with patch.object(lifecycle.requests, "delete", return_value=FakeResponse(status=404)):
            result = lifecycle.reconcile_retryable_meta_deletions(now=int(failed["next_retry_at"]))
        self.assertEqual(result["completed"], 1)
        self.assertEqual(lifecycle._operation(77)["state"], "completed")
        self.assertIsNone(lifecycle._link_by_room(room))

    def test_duplicate_bridge_leave_after_completion_has_no_second_remote_effect(self):
        room = self.seed()
        remote_delete = Mock(return_value=FakeResponse(status=204))
        with patch.object(lifecycle.requests, "delete", remote_delete):
            lifecycle.process_matrix_leave(room, self.trusted_leave())
            duplicate = lifecycle.process_matrix_leave(room, self.trusted_leave())
        self.assertEqual(remote_delete.call_count, 1)
        self.assertEqual(duplicate["reason"], "unmapped_room")

    def test_forged_membership_event_cannot_trigger_destructive_delete(self):
        room = self.seed()
        forged = self.trusted_leave()
        forged["timeline"]["events"][0]["state_key"] = "@victim:matrix.example.com"
        with patch.object(lifecycle.requests, "delete") as remote_delete:
            result = lifecycle.process_matrix_leave(room, forged)
        remote_delete.assert_not_called()
        self.assertEqual(result["reason"], "leave_not_from_bridge_bot")
        self.assertIsNotNone(lifecycle._link_by_room(room))


if __name__ == "__main__":
    unittest.main()
