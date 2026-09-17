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
        global admin_v2, patch_module, runtime_entrypoint, cookie_page, managed_page

        runtime_entrypoint = importlib.import_module("runtime_entrypoint")
        admin_v2 = importlib.import_module("admin_v2")
        patch_module = importlib.import_module("meta_admin_patch")
        cookie_page = importlib.import_module("meta_cookie_page")
        managed_page = importlib.import_module("nicegui_app")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_cookie_meta_is_first_class_admin_navigation_item(self):
        self.assertIn(("meta", "Facebook Messenger", "forum", "/admin/meta-cookie"), patch_module.NAV_ITEMS)

    def test_runtime_entrypoint_installs_native_admin_chrome(self):
        self.assertIs(admin_v2._admin_chrome, patch_module.admin_chrome)

    def test_admin_root_redirect_and_cookie_navigation_do_not_conflict(self):
        redirect_source = inspect.getsource(admin_v2._admin_entry_redirect)
        self.assertIn('request.url.path == "/admin"', redirect_source)
        self.assertIn('RedirectResponse("/admin/basic"', redirect_source)
        self.assertIn("/admin/meta-cookie", {item[3] for item in patch_module.NAV_ITEMS})

    def test_cookie_meta_page_is_supported_primary_surface(self):
        source = inspect.getsource(cookie_page.meta_cookie_page)
        self.assertIn('admin_v2._admin_chrome("meta")', source)
        self.assertIn("Copy as cURL", source)
        self.assertIn("Network / Red", source)
        self.assertIn("login_with_browser_cookies", source)

    def test_managed_meta_page_remains_available_for_testing(self):
        source = inspect.getsource(managed_page.meta_onboarding_page)
        self.assertIn('_admin_v2._admin_chrome("meta")', source)
        self.assertIn("create_pairing(saved_step)", source)
        self.assertIn("Abrir helper de Facebook", source)

    def test_visibility_no_longer_depends_on_legacy_admin_path_javascript(self):
        source = inspect.getsource(patch_module.admin_chrome)
        self.assertNotIn("window.location.pathname", source)
        self.assertNotIn("add_head_html", source)
        self.assertIn("ui.navigate.to", source)

    def test_runtime_entrypoint_registers_manual_cookie_page(self):
        source = inspect.getsource(runtime_entrypoint)
        self.assertIn("import meta_cookie_page", source)
        self.assertNotIn("meta_legacy_redirect.install()", source)


if __name__ == "__main__":
    unittest.main()
