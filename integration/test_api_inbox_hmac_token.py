import importlib
import os
import tempfile
import unittest
from unittest.mock import patch


class ApiInboxHmacTokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-hmac-contract-tests"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"

        global module, legacy
        runtime = importlib.import_module("final_app")
        module = importlib.import_module("runtime_enhancements")
        legacy = runtime.legacy
        legacy.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        with legacy.db() as conn:
            conn.execute("DELETE FROM settings")
        legacy.set_setting("chatwoot_base_url", "http://chatwoot.example.com")
        legacy.set_setting("chatwoot_account_id", "1")
        legacy.set_setting("chatwoot_inbox_id", "2")
        legacy.set_setting("chatwoot_api_token", "admin-token")

    def test_current_hmac_token_is_imported(self):
        details = {
            "id": 2,
            "channel_type": "Channel::Api",
            "webhook_url": "https://bridge.example.com/webhooks/chatwoot/inbox",
            "hmac_token": "current-token",
        }
        with patch.object(module, "api_inbox_details", return_value=details):
            result = module.verify_api_inbox_callback(
                "https://bridge.example.com/webhooks/chatwoot/inbox"
            )
        self.assertIn("verified", result)
        self.assertEqual(
            legacy.get_setting("chatwoot_api_inbox_signing_secret"), "current-token"
        )

    def test_stale_saved_token_cannot_make_verification_pass(self):
        legacy.set_setting("chatwoot_api_inbox_signing_secret", "stale-token")
        details = {
            "id": 2,
            "channel_type": "Channel::Api",
            "webhook_url": "https://bridge.example.com/webhooks/chatwoot/inbox",
        }
        with patch.object(module, "api_inbox_details", return_value=details):
            with self.assertRaisesRegex(RuntimeError, "HMAC token"):
                module.verify_api_inbox_callback(
                    "https://bridge.example.com/webhooks/chatwoot/inbox"
                )
        self.assertEqual(legacy.get_setting("api_inbox_callback_verified_at"), "")


if __name__ == "__main__":
    unittest.main()
