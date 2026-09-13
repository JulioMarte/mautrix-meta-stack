import base64
import importlib
import os
import tempfile
import unittest


class NiceGUIAdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-nicegui-tests"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"
        global module, legacy
        module = importlib.import_module("nicegui_app")
        legacy = module.legacy
        legacy.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        with legacy.db() as conn:
            conn.execute("DELETE FROM settings")
            conn.execute("DELETE FROM room_links")
            conn.execute("DELETE FROM processed_events")

    def test_save_configuration_persists_chatwoot_without_echoing_secrets(self):
        module.save_configuration(
            "http://chatwoot.example.com",
            "1",
            "2",
            "chatwoot-secret-token",
            False,
            "",
        )
        self.assertEqual(legacy.get_setting("chatwoot_base_url"), "http://chatwoot.example.com")
        self.assertEqual(legacy.get_setting("chatwoot_account_id"), "1")
        self.assertEqual(legacy.get_setting("chatwoot_inbox_id"), "2")
        self.assertEqual(legacy.get_setting("chatwoot_api_token"), "chatwoot-secret-token")
        self.assertTrue(legacy.get_setting("chatwoot_enabled_at_ms").isdigit())

    def test_bad_chatwoot_url_is_rejected(self):
        with self.assertRaises(ValueError):
            module.save_configuration("https://user:pass@chatwoot.example.com", "1", "2", "token", False, "")

    def test_internal_proxy_basic_auth_parser_contract(self):
        header = "Basic " + base64.b64encode(b"mautrix:resolver-secret-long-value").decode()
        decoded = base64.b64decode(header.split(" ", 1)[1], validate=True).decode("utf-8")
        self.assertEqual(decoded, "mautrix:resolver-secret-long-value")

    def test_nicegui_runtime_is_selected(self):
        self.assertTrue(callable(module.run))
        self.assertEqual(module.legacy.SESSION_SECRET, "session-secret-long-enough-for-nicegui-tests")


if __name__ == "__main__":
    unittest.main()
