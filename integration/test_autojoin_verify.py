import importlib
import os
import tempfile
import unittest
from unittest.mock import Mock, patch


class AutoJoinVerifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-autojoin-tests"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"
        global module, enhancements, legacy
        runtime = importlib.import_module("final_app")
        module = importlib.import_module("autojoin_verify")
        enhancements = importlib.import_module("runtime_enhancements")
        legacy = runtime.legacy
        legacy.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        with legacy.db() as conn:
            conn.execute("DELETE FROM settings")
        legacy.set_setting("auto_join_meta_portals", "1")

    @staticmethod
    def invite(sender="@metabot:matrix.example.com"):
        return {
            "invite_state": {
                "events": [{
                    "type": "m.room.member",
                    "state_key": "@admin:matrix.example.com",
                    "sender": sender,
                    "content": {"membership": "invite"},
                }]
            }
        }

    def test_join_is_verified_before_success(self):
        joined = Mock()
        joined.content = b'{"membership":"join"}'
        joined.json.return_value = {"membership": "join"}
        with patch.object(legacy, "bridge_bot_mxid", return_value="@metabot:matrix.example.com"), \
             patch.object(enhancements, "_matrix_post") as post, \
             patch.object(enhancements, "_matrix_get", return_value=joined) as get:
            self.assertTrue(module.robust_auto_join_room("!room:matrix.example.com", self.invite()))
        post.assert_called_once()
        get.assert_called_once()

    def test_membership_race_retries_until_join_persists(self):
        invited = Mock()
        invited.content = b'{"membership":"invite"}'
        invited.json.return_value = {"membership": "invite"}
        joined = Mock()
        joined.content = b'{"membership":"join"}'
        joined.json.return_value = {"membership": "join"}
        with patch.object(legacy, "bridge_bot_mxid", return_value="@metabot:matrix.example.com"), \
             patch.object(enhancements, "_matrix_post") as post, \
             patch.object(enhancements, "_matrix_get", side_effect=[invited, joined]), \
             patch.object(module.time, "sleep"):
            self.assertTrue(module.robust_auto_join_room("!room:matrix.example.com", self.invite()))
        self.assertEqual(post.call_count, 2)

    def test_untrusted_invite_is_never_joined(self):
        with patch.object(legacy, "bridge_bot_mxid", return_value="@metabot:matrix.example.com"), \
             patch.object(enhancements, "_matrix_post") as post:
            self.assertFalse(module.robust_auto_join_room("!room:matrix.example.com", self.invite("@evil:matrix.example.com")))
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
