import importlib
import os
import tempfile
import unittest
from unittest.mock import Mock, patch


class MetaPortalReconcileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-reconcile-tests"
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
        global module, autojoin, enhancements, legacy, prod
        runtime = importlib.import_module("final_app")
        module = importlib.import_module("meta_portal_reconcile")
        autojoin = importlib.import_module("autojoin_verify")
        enhancements = importlib.import_module("runtime_enhancements")
        legacy = runtime.legacy
        prod = runtime.prod
        legacy.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        with legacy.db() as conn:
            conn.execute("DELETE FROM settings")
            conn.execute("DELETE FROM room_links")
            conn.execute("DELETE FROM processed_events")
        prod._portal_cache.clear()
        legacy.set_setting("chatwoot_base_url", "http://chatwoot.example.com")
        legacy.set_setting("chatwoot_account_id", "1")
        legacy.set_setting("chatwoot_inbox_id", "2")
        legacy.set_setting("chatwoot_api_token", "token")
        legacy.set_setting("import_history_on_join", "1")
        legacy.set_setting("history_import_limit", "100")

    @staticmethod
    def bridge_state(room_name="Portal", sender="@metabot:matrix.example.com", bridgebot="@metabot:matrix.example.com"):
        return [
            {
                "type": "m.bridge",
                "state_key": "net.maunium.meta://facebook/123",
                "sender": sender,
                "content": {
                    "bridgebot": bridgebot,
                    "creator": "@meta_123:matrix.example.com",
                    "protocol": {"id": "facebook", "displayname": "Facebook Messenger"},
                    "channel": {"id": "123", "displayname": room_name},
                },
            }
        ]

    @staticmethod
    def invite_state(sender="@metabot:matrix.example.com"):
        return [
            {
                "type": "m.room.member",
                "state_key": "@admin:matrix.example.com",
                "sender": sender,
                "content": {"membership": "invite"},
            }
        ]

    def test_persisted_off_setting_cannot_disable_trusted_live_invite(self):
        legacy.set_setting("auto_join_meta_portals", "0")
        room = {"invite_state": {"events": self.invite_state()}}
        membership = Mock(content=b'{"membership":"join"}')
        membership.json.return_value = {"membership": "join"}
        with patch.object(enhancements, "_matrix_post") as join, \
             patch.object(enhancements, "_matrix_get", return_value=membership):
            self.assertTrue(autojoin.robust_auto_join_room("!new:matrix.example.com", room))
        join.assert_called_once()

    def test_verified_invite_is_joined_from_synapse_membership_inventory(self):
        memberships = {"!meta:matrix.example.com": "invite"}
        with patch.object(module, "user_memberships", return_value=memberships), \
             patch.object(module, "room_admin_state", return_value=self.bridge_state()), \
             patch.object(module, "_join_verified_portal", return_value=True) as join, \
             patch.object(enhancements, "import_recent_history", return_value=3), \
             patch.object(module, "_link_exists", return_value=True):
            result = module.reconcile_meta_portals()
        self.assertEqual(result["invited"], 1)
        self.assertEqual(result["joined"], 1)
        self.assertEqual(result["history_imported"], 3)
        join.assert_called_once_with("!meta:matrix.example.com")

    def test_trusted_metabot_invite_joins_even_without_bridge_state(self):
        memberships = {"!pending:matrix.example.com": "invite"}
        with patch.object(module, "user_memberships", return_value=memberships), \
             patch.object(module, "room_admin_state", return_value=self.invite_state()), \
             patch.object(module, "_join_verified_portal", return_value=True) as join, \
             patch.object(enhancements, "import_recent_history", return_value=1), \
             patch.object(module, "_link_exists", return_value=True):
            result = module.reconcile_meta_portals()
        self.assertEqual(result["invited"], 1)
        self.assertEqual(result["joined"], 1)
        self.assertEqual(result["ignored"], 0)
        join.assert_called_once_with("!pending:matrix.example.com")

    def test_trusted_exclusive_ghost_invite_joins_without_bridge_state(self):
        memberships = {"!pending-ghost:matrix.example.com": "invite"}
        with patch.object(module, "user_memberships", return_value=memberships), \
             patch.object(module, "room_admin_state", return_value=self.invite_state("@meta_123:matrix.example.com")), \
             patch.object(module, "_join_verified_portal", return_value=True) as join, \
             patch.object(enhancements, "import_recent_history", return_value=0), \
             patch.object(module, "_link_exists", return_value=False):
            result = module.reconcile_meta_portals()
        self.assertEqual(result["joined"], 1)
        self.assertEqual(result["ignored"], 0)
        join.assert_called_once_with("!pending-ghost:matrix.example.com")

    def test_untrusted_invite_without_bridge_state_is_never_joined(self):
        with patch.object(module, "user_memberships", return_value={"!random:matrix.example.com": "invite"}), \
             patch.object(module, "room_admin_state", return_value=self.invite_state("@evil:matrix.example.com")), \
             patch.object(module, "_join_verified_portal") as join:
            result = module.reconcile_meta_portals()
        self.assertEqual(result["ignored"], 1)
        self.assertEqual(result["joined"], 0)
        join.assert_not_called()

    def test_invite_with_no_membership_or_bridge_state_is_never_joined(self):
        with patch.object(module, "user_memberships", return_value={"!random:matrix.example.com": "invite"}), \
             patch.object(module, "room_admin_state", return_value=[]), \
             patch.object(module, "_join_verified_portal") as join:
            result = module.reconcile_meta_portals()
        self.assertEqual(result["ignored"], 1)
        self.assertEqual(result["joined"], 0)
        join.assert_not_called()

    def test_trusted_bridge_event_sender_is_authoritative_even_if_payload_bridgebot_differs(self):
        state = self.bridge_state(bridgebot="@legacybot:matrix.example.com")
        with patch.object(module, "room_admin_state", return_value=state):
            verified, reason = module.verified_meta_portal("!portal:matrix.example.com")
        self.assertTrue(verified)
        self.assertIn("trusted_bridge_state_sender", reason)

    def test_untrusted_sender_cannot_spoof_bridgebot_content(self):
        state = self.bridge_state(sender="@evil:matrix.example.com", bridgebot="@metabot:matrix.example.com")
        with patch.object(module, "room_admin_state", return_value=state):
            verified, reason = module.verified_meta_portal("!fake:matrix.example.com")
        self.assertFalse(verified)
        self.assertIn("untrusted_bridge_state_sender", reason)

    def test_exclusive_ghost_can_authoritatively_send_bridge_state(self):
        state = self.bridge_state(sender="@meta_123:matrix.example.com")
        with patch.object(module, "room_admin_state", return_value=state):
            verified, reason = module.verified_meta_portal("!legacy:matrix.example.com")
        self.assertTrue(verified)
        self.assertIn("exclusive_appservice_user_namespace", reason)

    def test_runtime_verifier_uses_same_authoritative_rule_and_caches_success(self):
        with patch.object(module, "verified_meta_portal", return_value=(True, "trusted")) as verify:
            self.assertTrue(module.runtime_is_bridge_portal("!runtime:matrix.example.com"))
            self.assertTrue(module.runtime_is_bridge_portal("!runtime:matrix.example.com"))
        verify.assert_called_once_with("!runtime:matrix.example.com")

    def test_old_joined_marketplace_portal_imports_history_without_new_sync_event(self):
        memberships = {"!marketplace:matrix.example.com": "join"}
        with patch.object(module, "user_memberships", return_value=memberships), \
             patch.object(module, "room_admin_state", return_value=self.bridge_state("Marketplace listing")), \
             patch.object(enhancements, "import_recent_history", return_value=7) as history, \
             patch.object(module, "_link_exists", side_effect=[False, True]):
            result = module.reconcile_meta_portals()
        self.assertEqual(result["history_imported"], 7)
        self.assertEqual(result["linked"], 1)
        history.assert_called_once_with("!marketplace:matrix.example.com")

    def test_non_meta_joined_room_is_not_imported(self):
        with patch.object(module, "user_memberships", return_value={"!personal:matrix.example.com": "join"}), \
             patch.object(module, "room_admin_state", return_value=[]), \
             patch.object(enhancements, "import_recent_history") as history:
            result = module.reconcile_meta_portals()
        self.assertEqual(result["history_imported"], 0)
        history.assert_not_called()

    def test_install_migrates_old_off_setting_and_forces_settings_api_on(self):
        legacy.set_setting("auto_join_meta_portals", "0")
        module.install(start_background=False)
        self.assertIs(prod.is_bridge_portal, module.runtime_is_bridge_portal)
        self.assertEqual(legacy.get_setting("auto_join_meta_portals"), "1")
        enhancements.save_operations_settings(
            auto_join=False,
            import_history=True,
            history_limit=25,
            sync_profiles=True,
            repair_deleted=True,
        )
        self.assertEqual(legacy.get_setting("auto_join_meta_portals"), "1")
        self.assertTrue(enhancements.operations_state()["auto_join"])


if __name__ == "__main__":
    unittest.main()
