import importlib
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch


class FakeResponse:
    def __init__(self, *, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.content = b"{}" if payload is not None else b""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class ChatwootTargetGuardV11Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-target-guard-tests"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"

        global runtime, legacy, lifecycle, guard, enhancements, nicegui_legacy
        runtime = importlib.import_module("final_app")
        lifecycle = importlib.import_module("conversation_lifecycle_v10")
        enhancements = importlib.import_module("runtime_enhancements")
        guard = importlib.import_module("chatwoot_target_guard_v11")
        nicegui_legacy = importlib.import_module("nicegui_legacy")
        legacy = runtime.legacy
        legacy.init_db()
        lifecycle.ensure_schema()
        guard.install()

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
        legacy.set_setting("chatwoot_api_inbox_signing_secret", "old-api-inbox-secret")
        legacy.set_setting("chatwoot_webhook_signing_secret", "old-account-webhook-secret")
        legacy.set_setting("api_inbox_callback_verified_at", "old")
        legacy.set_setting("api_inbox_delivery_verified_at", "old")
        legacy.set_setting("webhook_registration_verified_at", "old")
        legacy.set_setting("webhook_delivery_verified_at", "old")

    def seed_old_target_state(self, conversation_id=77, room_id="!old:matrix.example.com"):
        with legacy.db() as conn:
            conn.execute(
                "INSERT INTO room_links(room_id, contact_id, source_id, conversation_id, created_at) VALUES(?,?,?,?,?)",
                (room_id, 5, "source-old", conversation_id, int(time.time())),
            )
            conn.execute(
                "INSERT INTO processed_events(event_id, direction, processed_at) VALUES(?,?,?)",
                ("$old-event", "matrix_to_chatwoot", int(time.time())),
            )
        lifecycle.remember_verified_portal(room_id)
        lifecycle._start_operation(conversation_id, room_id, "meta", "completed")
        return room_id

    def test_real_nicegui_target_change_clears_links_dedupe_tombstones_and_secrets(self):
        room_id = self.seed_old_target_state()

        nicegui_legacy.save_configuration(
            "http://new-chatwoot.example.com", "9", "8", "new-token",
            False, "", "",
        )

        self.assertIsNone(runtime._linked_conversation_id(room_id))
        self.assertFalse(legacy.event_seen("$old-event"))
        self.assertIsNone(lifecycle._operation(77))
        self.assertEqual(legacy.get_setting("chatwoot_api_inbox_signing_secret"), "")
        self.assertEqual(legacy.get_setting("chatwoot_webhook_signing_secret"), "")
        self.assertEqual(legacy.get_setting("api_inbox_callback_verified_at"), "")
        self.assertEqual(legacy.get_setting("webhook_registration_verified_at"), "")
        # Matrix portal provenance is source-side identity and remains valid.
        self.assertTrue(lifecycle.portal_was_verified(room_id))

    def test_token_rotation_same_target_does_not_destroy_mappings_or_tombstones(self):
        room_id = self.seed_old_target_state()

        nicegui_legacy.save_configuration(
            "http://old-chatwoot.example.com", "1", "2", "rotated-personal-token",
            False, "", "",
        )

        self.assertEqual(runtime._linked_conversation_id(room_id), 77)
        self.assertIsNotNone(lifecycle._operation(77))
        self.assertEqual(legacy.get_setting("chatwoot_api_inbox_signing_secret"), "old-api-inbox-secret")
        self.assertEqual(legacy.get_setting("chatwoot_webhook_signing_secret"), "old-account-webhook-secret")

    def test_old_api_inbox_secret_cannot_authenticate_after_target_change(self):
        self.seed_old_target_state()
        nicegui_legacy.save_configuration(
            "http://new-chatwoot.example.com", "9", "8", "new-token",
            False, "", "",
        )

        with self.assertRaisesRegex(RuntimeError, "signing secret is not configured"):
            enhancements.verify_inbox_signature(b"{}", "sha256=stale", "2000000000", now=2000000000)

    def test_explicit_wrong_account_is_rejected_even_when_inbox_matches(self):
        payload = {
            "account": {"id": 999},
            "inbox": {"id": 2},
            "conversation": {"id": 77, "inbox_id": 2},
        }
        self.assertFalse(guard.configured_inbox_matches(payload))

    def test_missing_account_remains_compatible_when_inbox_matches(self):
        payload = {"conversation": {"id": 77, "inbox_id": 2}}
        self.assertTrue(guard.configured_inbox_matches(payload))

    def test_conversation_id_reuse_on_new_target_is_not_suppressed_by_old_tombstone(self):
        self.seed_old_target_state(conversation_id=77)
        nicegui_legacy.save_configuration(
            "http://new-chatwoot.example.com", "9", "8", "new-token",
            False, "", "",
        )

        new_room = "!new:matrix.example.com"
        with legacy.db() as conn:
            conn.execute(
                "INSERT INTO room_links(room_id, contact_id, source_id, conversation_id, created_at) VALUES(?,?,?,?,?)",
                (new_room, 6, "source-new", 77, int(time.time())),
            )
        lifecycle.remember_verified_portal(new_room)
        response = Mock(return_value=FakeResponse(payload={"event_id": "$delete-new-target"}))
        with patch.object(lifecycle.requests, "put", response):
            result = lifecycle.process_chatwoot_delete({
                "event": "conversation_deleted",
                "conversation_id": 77,
                "account": {"id": 9},
                "inbox": {"id": 8},
            })

        self.assertTrue(result["remote_requested"])
        self.assertEqual(lifecycle._operation(77)["origin"], "chatwoot")
        self.assertEqual(lifecycle._operation(77)["state"], "remote_requested")


if __name__ == "__main__":
    unittest.main()
