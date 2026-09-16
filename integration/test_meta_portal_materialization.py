import importlib
import os
import tempfile
import unittest
from unittest.mock import patch


class MetaPortalMaterializationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-materialization-tests"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"
        os.environ["MAUTRIX_REGISTRATION_PATH"] = os.path.join(cls.tmp.name, "registration.yaml")
        with open(os.environ["MAUTRIX_REGISTRATION_PATH"], "w", encoding="utf-8") as fh:
            fh.write(
                "id: meta\n"
                "sender_localpart: metabot\n"
                "namespaces:\n"
                "  users:\n"
                "    - regex: '^@meta_[0-9]+:matrix\\.example\\.com$'\n"
                "      exclusive: true\n"
                "  aliases: []\n"
                "  rooms: []\n"
            )
        runtime = importlib.import_module("final_app")
        cls.module = importlib.import_module("meta_portal_reconcile")
        cls.enhancements = importlib.import_module("runtime_enhancements")
        cls.legacy = runtime.legacy
        cls.legacy.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        with self.legacy.db() as conn:
            conn.execute("DELETE FROM settings")
            conn.execute("DELETE FROM room_links")
            conn.execute("DELETE FROM processed_events")
        self.legacy.set_setting("chatwoot_base_url", "http://chatwoot.example.com")
        self.legacy.set_setting("chatwoot_account_id", "1")
        self.legacy.set_setting("chatwoot_inbox_id", "2")
        self.legacy.set_setting("chatwoot_api_token", "token")
        self.legacy.set_setting("import_history_on_join", "1")

    @staticmethod
    def portal_state(*, space=False, include_member=True):
        events = [
            {
                "type": "m.room.create",
                "sender": "@metabot:matrix.example.com",
                "content": {"type": "m.space"} if space else {},
            },
            {
                "type": "m.bridge",
                "state_key": "net.maunium.meta://facebook/123",
                "sender": "@metabot:matrix.example.com",
                "content": {
                    "creator": "@meta_123:matrix.example.com",
                    "protocol": {"id": "facebook", "displayname": "Facebook Messenger"},
                    "channel": {"id": "123", "displayname": "Marketplace lead"},
                },
            },
        ]
        if include_member:
            events.append(
                {
                    "type": "m.room.member",
                    "state_key": "@meta_123:matrix.example.com",
                    "sender": "@metabot:matrix.example.com",
                    "content": {"membership": "join", "displayname": "Buyer"},
                }
            )
        return events

    def test_verified_room_without_history_is_materialized_in_chatwoot(self):
        room_id = "!marketplace:matrix.example.com"
        state = self.portal_state()
        with patch.object(self.module, "user_memberships", return_value={room_id: "join"}), \
             patch.object(self.module, "room_admin_state", return_value=state), \
             patch.object(self.enhancements, "import_recent_history", return_value=0), \
             patch.object(self.module, "_link_exists", side_effect=[False, False, True]), \
             patch.object(self.enhancements, "enhanced_ensure_room_link") as ensure:
            result = self.module.reconcile_meta_portals()
        ensure.assert_called_once_with(room_id, "@meta_123:matrix.example.com")
        self.assertEqual(result["materialized"], 1)
        self.assertEqual(result["linked"], 1)
        self.assertEqual(result["history_imported"], 0)

    def test_marketplace_space_is_never_materialized_as_conversation(self):
        room_id = "!space:matrix.example.com"
        with patch.object(self.module, "user_memberships", return_value={room_id: "join"}), \
             patch.object(self.module, "room_admin_state", return_value=self.portal_state(space=True)), \
             patch.object(self.enhancements, "import_recent_history") as history, \
             patch.object(self.enhancements, "enhanced_ensure_room_link") as ensure:
            result = self.module.reconcile_meta_portals()
        self.assertEqual(result["spaces_skipped"], 1)
        history.assert_not_called()
        ensure.assert_not_called()

    def test_trusted_bridge_creator_is_safe_fallback_when_member_state_is_missing(self):
        sender = self.module._portal_contact_sender_from_state(self.portal_state(include_member=False))
        self.assertEqual(sender, "@meta_123:matrix.example.com")

    def test_untrusted_creator_cannot_be_used_for_chatwoot_identity(self):
        state = self.portal_state(include_member=False)
        state[1]["content"]["creator"] = "@evil:matrix.example.com"
        self.assertEqual(self.module._portal_contact_sender_from_state(state), "")


if __name__ == "__main__":
    unittest.main()
