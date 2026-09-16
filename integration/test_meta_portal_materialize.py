import importlib
import os
import tempfile
import unittest
from unittest.mock import Mock, patch


class MetaPortalMaterializeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        defaults = {
            "DATA_DIR": cls.tmp.name,
            "MATRIX_SERVER_NAME": "matrix.example.com",
            "MATRIX_ADMIN_MXID": "@admin:matrix.example.com",
            "MATRIX_ADMIN_PASSWORD": "x" * 32,
            "INTEGRATION_ADMIN_PASSWORD": "x" * 32,
            "CHATWOOT_WEBHOOK_SECRET": "x" * 32,
            "INTEGRATION_SESSION_SECRET": "x" * 32,
            "META_PROXY_RESOLVER_SECRET": "x" * 32,
            "INTEGRATION_COOKIE_SECURE": "false",
            "START_MATRIX_SYNC": "false",
            "ALLOW_INSECURE_CHATWOOT": "true",
        }
        for key, value in defaults.items():
            os.environ[key] = value
        os.environ["MAUTRIX_REGISTRATION_PATH"] = os.path.join(cls.tmp.name, "registration.yaml")
        with open(os.environ["MAUTRIX_REGISTRATION_PATH"], "w", encoding="utf-8") as fh:
            fh.write("id: meta\nsender_localpart: metabot\nnamespaces:\n  users:\n    - regex: '^@meta_[0-9]+:matrix\\.example\\.com$'\n      exclusive: true\n  aliases: []\n  rooms: []\n")
        global module, reconcile, enhancements
        module = importlib.import_module("meta_portal_materialize")
        reconcile = importlib.import_module("meta_portal_reconcile")
        enhancements = importlib.import_module("runtime_enhancements")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @staticmethod
    def portal_state():
        return [{
            "type": "m.bridge",
            "state_key": "net.maunium.meta://facebook/123",
            "sender": "@metabot:matrix.example.com",
            "content": {
                "bridgebot": "@metabot:matrix.example.com",
                "protocol": {"id": "facebook"},
                "channel": {"id": "123", "displayname": "Customer"},
            },
        }]

    def test_materializes_verified_portal_without_new_message_ingest(self):
        room_id = "!portal:matrix.example.com"
        with patch.object(reconcile, "user_memberships", return_value={room_id: "join"}), \
             patch.object(reconcile, "_link_exists", side_effect=[False, True]), \
             patch.object(reconcile, "verified_meta_portal", return_value=(True, "bridge_state")), \
             patch.object(reconcile, "room_admin_state", return_value=self.portal_state()), \
             patch.object(module, "_sender_from_history", return_value="@meta_123:matrix.example.com"), \
             patch.object(enhancements, "enhanced_ensure_room_link", return_value={}) as ensure:
            result = module.materialize_meta_portals()
        self.assertEqual(result["materialized"], 1)
        ensure.assert_called_once_with(room_id, "@meta_123:matrix.example.com")

    def test_skips_space_instead_of_creating_fake_conversation(self):
        room_id = "!marketplace:matrix.example.com"
        state = self.portal_state() + [{"type": "m.room.create", "state_key": "", "content": {"type": "m.space"}}]
        with patch.object(reconcile, "user_memberships", return_value={room_id: "join"}), \
             patch.object(reconcile, "_link_exists", return_value=False), \
             patch.object(reconcile, "verified_meta_portal", return_value=(True, "bridge_state")), \
             patch.object(reconcile, "room_admin_state", return_value=state), \
             patch.object(enhancements, "enhanced_ensure_room_link") as ensure:
            result = module.materialize_meta_portals()
        self.assertEqual(result["skipped_space"], 1)
        ensure.assert_not_called()

    def test_history_uses_remote_ghost_not_admin_or_bridge_bot(self):
        response = Mock(content=b"1")
        response.json.return_value = {"chunk": [
            {"type": "m.room.message", "sender": "@admin:matrix.example.com"},
            {"type": "m.room.message", "sender": "@metabot:matrix.example.com"},
            {"type": "m.room.message", "sender": "@meta_456:matrix.example.com"},
        ]}
        with patch.object(enhancements, "_matrix_get", return_value=response):
            self.assertEqual(module._sender_from_history("!portal:matrix.example.com"), "@meta_456:matrix.example.com")


if __name__ == "__main__":
    unittest.main()
