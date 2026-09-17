import importlib
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
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
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def json(self):
        return self._payload


class ConversationLifecycleAdversarialV11Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_SERVER_NAME"] = "matrix.example.com"
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-adversarial-lifecycle-tests"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"
        registration_path = Path(cls.tmp.name) / "registration.yaml"
        os.environ["MAUTRIX_REGISTRATION_PATH"] = str(registration_path)
        with registration_path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(
                {
                    "id": "meta",
                    "sender_localpart": "metabot",
                    "as_token": "adversarial-as-token",
                    "hs_token": "adversarial-hs-token",
                    "namespaces": {
                        "users": [{"regex": r"^@meta_[0-9]+:matrix\\.example\\.com$", "exclusive": True}],
                        "aliases": [],
                        "rooms": [],
                    },
                },
                fh,
                sort_keys=False,
            )

        global runtime, legacy, lifecycle, hardening
        runtime = importlib.import_module("final_app")
        lifecycle = importlib.import_module("conversation_lifecycle_v10")
        hardening = importlib.import_module("conversation_lifecycle_hardening_v11")
        legacy = runtime.legacy
        legacy.init_db()
        lifecycle.ensure_schema()
        hardening.install()
        if legacy.bridge_bot_mxid() != "@metabot:matrix.example.com":
            raise AssertionError(f"unexpected bridge bot fixture: {legacy.bridge_bot_mxid()!r}")

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
        legacy.set_setting("matrix_next_batch", "s1")
        self.matrix_headers = patch.object(
            legacy,
            "matrix_headers",
            return_value={"Authorization": "Bearer adversarial-test-token"},
        )
        self.matrix_headers.start()

    def tearDown(self):
        self.matrix_headers.stop()

    def seed(self, room="!portal:matrix.example.com", conversation=77, verified=True):
        with legacy.db() as conn:
            conn.execute(
                "INSERT INTO room_links(room_id, contact_id, source_id, conversation_id, created_at) VALUES(?,?,?,?,?)",
                (room, 5, "source", conversation, int(time.time())),
            )
        if verified:
            lifecycle.remember_verified_portal(room)
        return room

    def trusted_leave(self, *, sender="@metabot:matrix.example.com", state_key=None, membership="leave"):
        return {
            "timeline": {
                "events": [{
                    "type": "m.room.member",
                    "state_key": state_key or legacy.MATRIX_ADMIN_MXID,
                    "sender": sender,
                    "content": {"membership": membership},
                }]
            }
        }

    def delete_payload(self, conversation=77, account=1, inbox=2):
        return {
            "event": "conversation_deleted",
            "conversation_id": conversation,
            "account": {"id": account},
            "inbox": {"id": inbox},
        }

    def test_chatwoot_callback_losing_origin_race_to_meta_emits_no_matrix_delete(self):
        room = self.seed()
        original_start = lifecycle._start_operation

        def meta_wins(conversation_id, room_id, _origin, _state):
            original_start(conversation_id, room_id, "meta", "remote_confirmed")

        with patch.object(lifecycle, "_start_operation", side_effect=meta_wins), \
             patch.object(lifecycle.requests, "put") as matrix_put:
            result = hardening.process_chatwoot_delete(self.delete_payload())

        matrix_put.assert_not_called()
        self.assertEqual(result["reason"], "meta_delete_loop_suppressed")
        operation = lifecycle._operation(77)
        self.assertEqual(operation["origin"], "meta")
        self.assertEqual(operation["room_id"], room)

    def test_meta_leave_losing_origin_race_to_chatwoot_becomes_confirmation(self):
        room = self.seed()
        original_start = lifecycle._start_operation

        def chatwoot_wins(conversation_id, room_id, _origin, _state):
            original_start(conversation_id, room_id, "chatwoot", "pending")

        with patch.object(lifecycle, "_start_operation", side_effect=chatwoot_wins), \
             patch.object(lifecycle.requests, "delete") as chatwoot_delete:
            result = hardening.process_matrix_leave(room, self.trusted_leave())

        chatwoot_delete.assert_not_called()
        self.assertEqual(result["origin"], "chatwoot")
        self.assertEqual(lifecycle._operation(77)["state"], "completed")
        self.assertIsNone(lifecycle._link_by_room(room))

    def test_replayed_meta_leave_respects_future_backoff(self):
        room = self.seed()
        lifecycle._start_operation(77, room, "meta", "remote_confirmed")
        lifecycle._update_operation(
            77,
            state="failed_retryable",
            error="upstream unavailable",
            increment_attempts=True,
            next_retry_at=500,
        )

        with patch.object(hardening.time, "time", return_value=100), \
             patch.object(lifecycle.requests, "delete") as chatwoot_delete:
            result = hardening.process_matrix_leave(room, self.trusted_leave())

        chatwoot_delete.assert_not_called()
        self.assertEqual(result["reason"], "retry_scheduled")
        self.assertEqual(result["next_retry_at"], 500)
        self.assertIsNotNone(lifecycle._link_by_room(room))

    def test_ambiguous_chatwoot_timeout_then_404_retry_completes_idempotently(self):
        room = self.seed()
        with patch.object(lifecycle.requests, "delete", side_effect=requests.Timeout("socket timed out")):
            with self.assertRaises(requests.Timeout):
                hardening.process_matrix_leave(room, self.trusted_leave())

        operation = lifecycle._operation(77)
        self.assertEqual(operation["state"], "failed_retryable")
        self.assertIsNotNone(lifecycle._link_by_room(room))

        with patch.object(lifecycle.requests, "delete", return_value=FakeResponse(status=404)):
            result = lifecycle.reconcile_retryable_meta_deletions(now=int(operation["next_retry_at"]))

        self.assertEqual(result["completed"], 1)
        self.assertEqual(lifecycle._operation(77)["state"], "completed")
        self.assertIsNone(lifecycle._link_by_room(room))

    def test_process_death_after_matrix_accept_uses_same_transaction_on_retry(self):
        room = self.seed()
        urls = []

        def matrix_accept(url, **_kwargs):
            urls.append(url)
            return FakeResponse(payload={"event_id": "$same-delete-event"})

        original_update = lifecycle._update_operation

        def crash_before_remote_requested(conversation_id, **kwargs):
            if kwargs.get("state") == "remote_requested":
                raise SystemExit("simulated process death")
            return original_update(conversation_id, **kwargs)

        with patch.object(lifecycle.requests, "put", side_effect=matrix_accept), \
             patch.object(lifecycle, "_update_operation", side_effect=crash_before_remote_requested):
            with self.assertRaises(SystemExit):
                hardening.process_chatwoot_delete(self.delete_payload())

        self.assertEqual(lifecycle._operation(77)["state"], "pending")
        self.assertIsNotNone(lifecycle._link_by_room(room))

        with patch.object(lifecycle.requests, "put", side_effect=matrix_accept):
            result = hardening.process_chatwoot_delete(self.delete_payload())

        self.assertTrue(result["remote_requested"])
        self.assertEqual(len(urls), 2)
        self.assertEqual(urls[0], urls[1])
        self.assertIn("/send/com.beeper.delete_chat/cwdel-", urls[0])

    def test_matrix_200_without_event_id_is_retryable_and_mapping_survives(self):
        room = self.seed()
        with patch.object(lifecycle.requests, "put", return_value=FakeResponse(payload={})):
            with self.assertRaisesRegex(RuntimeError, "no event_id"):
                hardening.process_chatwoot_delete(self.delete_payload())

        operation = lifecycle._operation(77)
        self.assertEqual(operation["state"], "failed_retryable")
        self.assertIsNotNone(lifecycle._link_by_room(room))

        with patch.object(
            lifecycle.requests,
            "put",
            return_value=FakeResponse(payload={"event_id": "$retry-delete"}),
        ):
            result = hardening.process_chatwoot_delete(self.delete_payload())
        self.assertTrue(result["remote_requested"])
        self.assertEqual(lifecycle._operation(77)["state"], "remote_requested")

    def test_malformed_or_spoofed_leaves_cannot_delete_chatwoot(self):
        room = self.seed()
        cases = [
            self.trusted_leave(sender=legacy.MATRIX_ADMIN_MXID),
            self.trusted_leave(state_key="@someone:matrix.example.com"),
            self.trusted_leave(membership="join"),
        ]
        with patch.object(lifecycle.requests, "delete") as chatwoot_delete:
            for candidate in cases:
                result = hardening.process_matrix_leave(room, candidate)
                self.assertEqual(result["reason"], "leave_not_from_bridge_bot")
        chatwoot_delete.assert_not_called()
        self.assertIsNotNone(lifecycle._link_by_room(room))

    def test_concurrent_duplicate_chatwoot_callbacks_emit_one_matrix_delete(self):
        self.seed()
        entered = threading.Event()
        release = threading.Event()
        calls = []
        results = []
        errors = []

        def matrix_put(url, **_kwargs):
            calls.append(url)
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test did not release Matrix request")
            return FakeResponse(payload={"event_id": "$concurrent-delete"})

        def worker():
            try:
                results.append(hardening.process_chatwoot_delete(self.delete_payload()))
            except BaseException as exc:
                errors.append(exc)

        with patch.object(lifecycle.requests, "put", side_effect=matrix_put):
            first = threading.Thread(target=worker)
            second = threading.Thread(target=worker)
            first.start()
            self.assertTrue(entered.wait(timeout=5), "first callback never reached Matrix")
            second.start()
            release.set()
            first.join(timeout=5)
            second.join(timeout=5)

        self.assertFalse(first.is_alive() or second.is_alive(), "concurrent callbacks deadlocked")
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(bool(item.get("remote_requested")) for item in results), 1)
        self.assertEqual(sum(bool(item.get("duplicate")) for item in results), 1)
        operation = lifecycle._operation(77)
        self.assertEqual(operation["state"], "remote_requested")
        self.assertEqual(operation["attempts"], 1)

    def test_wrong_account_delete_callback_is_ignored_without_matrix_effect(self):
        self.seed()
        with patch.object(lifecycle.requests, "put") as matrix_put:
            result = hardening.process_chatwoot_delete(self.delete_payload(account=999))
        matrix_put.assert_not_called()
        self.assertEqual(result["reason"], "outside_configured_chatwoot_account")

    def test_retry_reconciler_busy_lock_produces_no_remote_effect(self):
        room = self.seed()
        lifecycle._start_operation(77, room, "meta", "remote_confirmed")
        busy_lock = Mock()
        busy_lock.acquire.return_value = False
        with patch.object(lifecycle, "_RECONCILE_LOCK", busy_lock), \
             patch.object(lifecycle.requests, "delete") as chatwoot_delete:
            result = lifecycle.reconcile_retryable_meta_deletions(now=0)
        chatwoot_delete.assert_not_called()
        busy_lock.release.assert_not_called()
        self.assertTrue(result["busy"])
        self.assertIsNotNone(lifecycle._link_by_room(room))


if __name__ == "__main__":
    unittest.main()
