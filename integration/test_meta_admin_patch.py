import importlib
import inspect
import os
import tempfile
import unittest


class MetaAdminPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-meta-nav-tests"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["MAUTRIX_PROVISIONING_SECRET"] = "provisioning-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"
        global admin_v2, patch_module, nicegui_app

        # Import exactly the module Compose executes in production. This must be
        # sufficient to install the Facebook sidebar; no wrapper import is allowed
        # to make this assertion pass accidentally.
        nicegui_app = importlib.import_module("nicegui_app")
        admin_v2 = importlib.import_module("admin_v2")
        patch_module = importlib.import_module("meta_admin_patch")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_meta_is_first_class_admin_navigation_item(self):
        self.assertIn(("meta", "Facebook Messenger", "forum", "/admin/meta"), patch_module.NAV_ITEMS)

    def test_direct_nicegui_app_startup_installs_native_admin_chrome(self):
        self.assertIs(admin_v2._admin_chrome, patch_module.admin_chrome)

    def test_admin_root_redirect_and_meta_navigation_do_not_conflict(self):
        redirect_source = inspect.getsource(admin_v2._admin_entry_redirect)
        self.assertIn('request.url.path == "/admin"', redirect_source)
        self.assertIn('RedirectResponse("/admin/basic"', redirect_source)
        self.assertIn("/admin/meta", {item[3] for item in patch_module.NAV_ITEMS})

    def test_meta_page_is_registered_and_uses_admin_v2_chrome(self):
        self.assertTrue(callable(nicegui_app.meta_onboarding_page))
        source = inspect.getsource(nicegui_app.meta_onboarding_page)
        self.assertIn('_admin_v2._admin_chrome("meta")', source)
        self.assertNotIn("_legacy_ui.page_shell", source)

    def test_visibility_no_longer_depends_on_legacy_admin_path_javascript(self):
        source = inspect.getsource(patch_module.admin_chrome)
        self.assertNotIn("window.location.pathname", source)
        self.assertNotIn("add_head_html", source)
        self.assertIn("ui.navigate.to", source)

    def test_helper_free_mobile_login_is_preferred_when_available(self):
        options = {
            "facebook": "Facebook web",
            "messenger": "Messenger web",
            "messenger-lite": "Messenger iOS",
            "messenger-lite-android": "Messenger Android",
        }
        self.assertEqual(nicegui_app._preferred_flow(options), "messenger-lite-android")
        self.assertEqual(
            nicegui_app._preferred_flow({"facebook": "Facebook", "messenger-lite": "Messenger iOS"}),
            "messenger-lite",
        )


if __name__ == "__main__":
    unittest.main()
