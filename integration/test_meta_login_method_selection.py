import importlib
import inspect
import os
import tempfile
import unittest


class MetaLoginMethodSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-selection-tests"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["MAUTRIX_PROVISIONING_SECRET"] = "provisioning-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"
        cls.module = importlib.import_module("nicegui_app")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_recommended_flows_are_ordered_without_forcing_a_selection(self):
        ordered = self.module._ordered_flow_options({
            "facebook": "Facebook web",
            "other": "Other",
            "messenger-lite-android": "Android",
            "messenger-lite": "iOS",
        })
        self.assertEqual(
            list(ordered),
            ["messenger-lite-android", "messenger-lite", "facebook", "other"],
        )

    def test_method_selector_starts_login_directly_and_has_no_extra_start_button(self):
        source = inspect.getsource(self.module.meta_onboarding_page)
        self.assertIn("on_change=start_login", source)
        self.assertIn("value=None", source)
        self.assertIn("Selecciona un método y la conexión comenzará automáticamente", source)
        self.assertNotIn('ui.button("Iniciar conexión"', source)

    def test_active_attempt_blocks_starting_a_second_flow(self):
        source = inspect.getsource(self.module.meta_onboarding_page)
        self.assertIn("if saved_step:", source)
        self.assertIn("Ya hay un intento de conexión en curso", source)
        self.assertIn("elif flow_options:", source)


if __name__ == "__main__":
    unittest.main()
