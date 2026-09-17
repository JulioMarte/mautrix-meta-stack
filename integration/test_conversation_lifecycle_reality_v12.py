import hashlib
import hmac
import importlib
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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


class ConversationLifecycleRealityV12Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_SERVER_NAME"] = "matrix.example.com"
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-reality-v12-tests"
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
                    "as_token": "reality-as-token",
                    "hs_token": "reality-hs-token",
                    "namespaces": {
                        "users": [{"regex": r"^@meta_[0-9]+:matrix\\.example\\.com$", "exclusive": True}],
                        "aliases": [],
                        "rooms": [],
                    },
                },
                fh,
                sort_keys=False,
            )

        global runtime, legacy, lifecycle, hardening, guard, enhancements, nicegui_legacy
        runtime = importlib.import_module("final_app")
        lifecycle = importlib.import_module("conversation_lifecycle_v10")
        hardening = importlib.import_module("conversation_lifecycle_hardening_v11")
        enhancements = importlib.import_module("runtime_enhancements")
        guard = importlib.import_module("chatwoot_target_guard_v11")
        nicegui_legacy = importlib.import_module("nicegui_legacy")
        legacy = runtime.legacy
        legacy.init_db()
        lifecycle.ensure_schema()
        hardening.install()
        guard.install()
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
        legacy.set_setting("chatwoot_base_url", "http://old-chatwoot.example.com")
        legacy.set_setting("chatwoot_account_id", "1")
        legacy.set_setting("chatwoot_inbox_id", "2")
        legacy.set_setting("chatwoot_api_token", "old-token")
        legacy.set_setting("chatwoot_api_inbox_signing_secret", "api-inbox-secret")
        legacy.set_setting("matrix_next_batch", "s1")
        legacy.set_setting("chatwoot_target_reconfiguring", "0")
        guard._VERIFIED_API_INBOX_TARGET.set(None)
        self.matrix_headers = patch.object(
            legacy,
            "matrix_headers",
            return_value={"Authorization": "Bearer reality-test-token"},
        )
        self.matrix_headers.start()

    def tearDown(self):
        guard._VERIFIED_API_INBOX_TARGET.set(None)
        self.matrix_headers.stop()

    def seed(self, room="!portal:matrix.example.com", conversation=77):
        with legacy.db() as conn:
            conn.execute(
                "INSERT INTO room_links(room_id, contact_id, source_id, conversation_id, created_at) VALUES(?,?,?,?,?)",
                (room, 5, "source", conversation, int(time.time())),
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

    def delete_payload(self, conversation=77):
        return {
            "event": "conversation_deleted",
            "conversation_id": conversation,
            "account": {"id": 1},
            "inbox": {"id": 2},
        }

    def test_target_switch_waits_for_inflight_meta_delete_and_never_redirects_it(self):
        room = self.seed()
        delete_entered = threading.Event()
        release_delete = threading.Event()
        switch_finished = threading.Event()
        delete_urls = []
        errors = []

        def blocking_delete(url, **_kwargs):
            delete_urls.append(url)
            delete_entered.set()
            if not release_delete.wait(timeout=5):
                raise RuntimeError("test did not release old-target DELETE")
            return FakeResponse(status=204)

        def do_delete():
            try:
                hardening.process_matrix_leave(room, self.trusted_leave())
            except BaseException as exc:
                errors.append(exc)

        def switch_target():
            try:
                nicegui_legacy.save_configuration(
                    "http://new-chatwoot.example.com", "9", "8", "new-token",
                    False, "", "",
                )
                switch_finished.set()
            except BaseException as exc:
                errors.append(exc)

        with patch.object(lifecycle.requests, "delete", side_effect=blocking_delete):
            delete_thread = threading.Thread(target=do_delete)
            switch_thread = threading.Thread(target=switch_target)
            delete_thread.start()
            self.assertTrue(delete_entered.wait(timeout=5), "Meta delete never reached Chatwoot")
            switch_thread.start()
            time.sleep(0.1)
            self.assertFalse(switch_finished.is_set(), "target switch crossed an in-flight destructive DELETE")
            self.assertEqual(legacy.get_setting("chatwoot_base_url"), "http://old-chatwoot.example.com")
            release_delete.set()
            delete_thread.join(timeout=5)
            switch_thread.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(len(delete_urls), 1)
        self.assertTrue(delete_urls[0].startswith("http://old-chatwoot.example.com/"), delete_urls[0])
        self.assertEqual(legacy.get_setting("chatwoot_base_url"), "http://new-chatwoot.example.com")
        self.assertEqual(legacy.get_setting("chatwoot_account_id"), "9")
        self.assertEqual(legacy.get_setting("chatwoot_inbox_id"), "8")
        self.assertIsNone(lifecycle._link_by_room(room))
        self.assertIsNone(lifecycle._operation(77))

    def test_old_meta_leave_after_target_switch_cannot_delete_new_target(self):
        room = self.seed()
        nicegui_legacy.save_configuration(
            "http://new-chatwoot.example.com", "9", "8", "new-token",
            False, "", "",
        )
        with patch.object(lifecycle.requests, "delete") as chatwoot_delete:
            result = hardening.process_matrix_leave(room, self.trusted_leave())
        chatwoot_delete.assert_not_called()
        self.assertEqual(result["reason"], "unmapped_room")

    def test_auth_rate_limit_and_server_failures_never_finalize_local_delete(self):
        for status in (401, 403, 429, 500, 503):
            with self.subTest(status=status):
                with legacy.db() as conn:
                    conn.execute("DELETE FROM room_links")
                    conn.execute("DELETE FROM conversation_deletions")
                room = self.seed()
                with patch.object(lifecycle.requests, "delete", return_value=FakeResponse(status=status)):
                    with self.assertRaises(requests.HTTPError):
                        hardening.process_matrix_leave(room, self.trusted_leave())
                operation = lifecycle._operation(77)
                self.assertIsNotNone(operation)
                self.assertEqual(operation["origin"], "meta")
                self.assertEqual(operation["state"], "failed_retryable")
                self.assertGreater(int(operation["next_retry_at"]), 0)
                self.assertIsNotNone(lifecycle._link_by_room(room))

    def test_sequential_replay_of_chatwoot_delete_emits_one_matrix_effect(self):
        self.seed()
        response = FakeResponse(payload={"event_id": "$delete-once"})
        with patch.object(lifecycle.requests, "put", return_value=response) as matrix_put:
            first = hardening.process_chatwoot_delete(self.delete_payload())
            second = hardening.process_chatwoot_delete(self.delete_payload())
        self.assertTrue(first["remote_requested"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["state"], "remote_requested")
        matrix_put.assert_called_once()

    def test_stale_or_tampered_hmac_never_binds_a_destructive_callback_target(self):
        secret = "api-inbox-secret"
        valid_body = b'{"event":"conversation_deleted","conversation_id":77}'
        timestamp = "2000000000"
        signature = "sha256=" + hmac.new(
            secret.encode(), timestamp.encode() + b"." + valid_body, hashlib.sha256
        ).hexdigest()

        with self.assertRaisesRegex(RuntimeError, "too old|too far"):
            guard.verify_api_inbox_signature(valid_body, signature, timestamp, now=2000001000)
        self.assertIsNone(guard._VERIFIED_API_INBOX_TARGET.get())

        tampered = b'{"event":"conversation_deleted","conversation_id":78}'
        with self.assertRaisesRegex(RuntimeError, "Invalid Chatwoot API inbox webhook signature"):
            guard.verify_api_inbox_signature(tampered, signature, timestamp, now=2000000000)
        self.assertIsNone(guard._VERIFIED_API_INBOX_TARGET.get())

    def test_duplicate_leave_after_completed_meta_delete_has_no_second_http_effect(self):
        room = self.seed()
        with patch.object(lifecycle.requests, "delete", return_value=FakeResponse(status=204)) as chatwoot_delete:
            first = hardening.process_matrix_leave(room, self.trusted_leave())
            second = hardening.process_matrix_leave(room, self.trusted_leave())
        self.assertTrue(first["completed"])
        self.assertEqual(second["reason"], "unmapped_room")
        chatwoot_delete.assert_called_once()


if __name__ == "__main__":
    unittest.main()
