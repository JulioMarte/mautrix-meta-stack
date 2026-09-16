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
        global admin_v2, patch_module, runtime_entrypoint, cookie_page

        # Import exactly the module Compose executes in production. It must both
        # install the Facebook sidebar and register the cookie-first page.
        runtime_entrypoint = importlib.import_module("runtime_entrypoint")
        admin_v2 = importlib.import_module("admin_v2")
        patch_module = importlib.import_module("meta_admin_patch")
        cookie_page = importlib.import_module("meta_cookie_page")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_meta_is_first_class_admin_navigation_item(self):
        self.assertIn(("meta", "Facebook Messenger", "forum", "/admin/meta-cookie"), patch_module.NAV_ITEMS)

    def test_runtime_entrypoint_installs_native_admin_chrome(self):
        self.assertIs(admin_v2._admin_chrome, patch_module.admin_chrome)

    def test_admin_root_redirect_and_meta_navigation_do_not_conflict(self):
        redirect_source = inspect.getsource(admin_v2._admin_entry_redirect)
        self.assertIn('request.url.path == "/admin"', redirect_source)
        self.assertIn('RedirectResponse("/admin/basic"', redirect_source)
        self.assertIn("/admin/meta-cookie", {item[3] for item in patch_module.NAV_ITEMS})

    def test_cookie_meta_page_is_registered_and_uses_admin_v2_chrome(self):
        self.assertTrue(callable(cookie_page.meta_cookie_page))
        source = inspect.getsource(cookie_page.meta_cookie_page)
        self.assertIn('admin_v2._admin_chrome("meta")', source)
        self.assertIn("login_with_browser_cookies", source)
        self.assertNotIn("create_pairing", source)

    def test_visibility_no_longer_depends_on_legacy_admin_path_javascript(self):
        source = inspect.getsource(patch_module.admin_chrome)
        self.assertNotIn("window.location.pathname", source)
        self.assertNotIn("add_head_html", source)
        self.assertIn("ui.navigate.to", source)

    def test_runtime_entrypoint_registers_cookie_page(self):
        source = inspect.getsource(runtime_entrypoint)
        self.assertIn("import meta_cookie_page", source)


if __name__ == "__main__":
    unittest.main()
