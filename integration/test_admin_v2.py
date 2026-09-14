import importlib
import os
import tempfile
import unittest
from unittest.mock import Mock, patch


class AdminV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-admin-v2-tests"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"
        global module, runtime, legacy, enhancements, reconciler
        runtime = importlib.import_module("final_app")
        module = importlib.import_module("admin_v2")
        enhancements = importlib.import_module("runtime_enhancements")
        reconciler = importlib.import_module("meta_portal_reconcile")
        legacy = runtime.legacy
        legacy.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        with legacy.db() as conn:
            conn.execute("DELETE FROM settings")
            conn.execute("DELETE FROM room_links")
            conn.execute("DELETE FROM processed_events")
        legacy.set_setting("chatwoot_base_url", "http://chatwoot.example.com")
        legacy.set_setting("chatwoot_account_id", "1")
        legacy.set_setting("chatwoot_inbox_id", "2")
        legacy.set_setting("chatwoot_api_token", "token")

    @staticmethod
    def invite_room():
        return {
            "invite_state": {
                "events": [
                    {
                        "type": "m.room.member",
                        "state_key": "@admin:matrix.example.com",
                        "sender": "@metabot:matrix.example.com",
                        "content": {"membership": "invite"},
                    }
                ]
            }
        }

    def test_initial_sync_processes_invites_before_checkpoint(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "next_batch": "s1",
            "rooms": {
                "invite": {"!new:matrix.example.com": self.invite_room()},
                "join": {
                    "!old:matrix.example.com": {
                        "timeline": {"events": [{"event_id": "$old", "type": "m.room.message"}]}
                    }
                },
            },
        }
        with patch.object(module.requests, "get", return_value=response), \
             patch.object(legacy, "matrix_headers", return_value={"Authorization": "Bearer test"}), \
             patch.object(enhancements, "auto_join_room", return_value=True) as auto_join, \
             patch.object(enhancements, "import_recent_history", return_value=1) as history, \
             patch.object(enhancements, "enhanced_live_matrix_event") as live:
            module.reliable_sync_once()
        auto_join.assert_called_once()
        history.assert_called_once_with("!new:matrix.example.com")
        live.assert_not_called()
        self.assertEqual(legacy.get_setting("matrix_next_batch"), "s1")
        self.assertTrue(legacy.get_setting("last_meta_auto_join_at"))

    def test_incremental_sync_processes_invites_and_live_timeline(self):
        legacy.set_setting("matrix_next_batch", "s0")
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "next_batch": "s2",
            "rooms": {
                "invite": {"!new:matrix.example.com": self.invite_room()},
                "join": {
                    "!joined:matrix.example.com": {
                        "timeline": {"events": [{"event_id": "$new", "type": "m.room.message"}]}
                    }
                },
            },
        }
        with patch.object(module.requests, "get", return_value=response), \
             patch.object(legacy, "matrix_headers", return_value={"Authorization": "Bearer test"}), \
             patch.object(enhancements, "auto_join_room", return_value=True), \
             patch.object(enhancements, "import_recent_history", return_value=0), \
             patch.object(enhancements, "enhanced_live_matrix_event") as live:
            module.reliable_sync_once()
        live.assert_called_once()
        self.assertEqual(legacy.get_setting("matrix_next_batch"), "s2")

    def test_reconcile_pending_invites_does_not_touch_live_checkpoint(self):
        legacy.set_setting("matrix_next_batch", "live-token")
        with patch.object(reconciler, "user_memberships", return_value={"!one:matrix.example.com": "invite"}), \
             patch.object(reconciler, "verified_meta_portal", return_value=(True, "test")), \
             patch.object(reconciler, "_join_verified_portal", return_value=True), \
             patch.object(enhancements, "import_recent_history", return_value=0), \
             patch.object(reconciler, "_link_exists", return_value=False):
            result = module.reconcile_pending_meta_invites()
        self.assertEqual(result["joined"], 1)
        self.assertEqual(result["invited"], 1)
        self.assertIn("checked_at", result)
        self.assertEqual(legacy.get_setting("matrix_next_batch"), "live-token")

    def test_basic_connection_change_invalidates_api_callback_verification(self):
        legacy.set_setting("api_inbox_callback_verified_at", "old")
        legacy.set_setting("api_inbox_delivery_verified_at", "old")
        module.save_basic_connection("http://chatwoot.example.com", "token", "1", "3")
        self.assertEqual(legacy.get_setting("chatwoot_inbox_id"), "3")
        self.assertEqual(legacy.get_setting("api_inbox_callback_verified_at"), "")
        self.assertEqual(legacy.get_setting("api_inbox_delivery_verified_at"), "")

    def test_old_account_webhook_parser_only_matches_connector_url(self):
        payload = {"payload": {"webhooks": [
            {"id": 1, "url": "https://bridge.example.com/webhooks/chatwoot"},
            {"id": 2, "url": "https://other.example.com/hook"},
        ]}}
        with patch.object(module, "_chatwoot_request", return_value=payload):
            hooks = module.legacy_account_webhooks("https://bridge.example.com/webhooks/chatwoot")
        self.assertEqual([hook["id"] for hook in hooks], [1])


if __name__ == "__main__":
    unittest.main()
