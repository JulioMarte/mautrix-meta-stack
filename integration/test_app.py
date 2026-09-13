import importlib
import os
import tempfile
import unittest


class IntegrationAdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-tests"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        global module
        module = importlib.import_module("app")
        module.init_db()
        cls.client = module.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        with module.db() as conn:
            conn.execute("DELETE FROM settings")
            conn.execute("DELETE FROM room_links")
            conn.execute("DELETE FROM processed_events")
        with self.client.session_transaction() as sess:
            sess.clear()

    def login(self):
        response = self.client.post("/admin/login", data={"password": "admin-password"})
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as sess:
            return sess["csrf"]

    def test_admin_requires_login(self):
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

    def test_csrf_is_required_for_mutation(self):
        self.login()
        response = self.client.post("/admin/settings", data={})
        self.assertEqual(response.status_code, 403)

    def test_proxy_can_be_enabled_and_secret_is_not_rendered(self):
        csrf = self.login()
        secret_proxy = "http://alice:super-secret@proxy.example.com:8080"
        response = self.client.post(
            "/admin/settings",
            data={
                "csrf": csrf,
                "chatwoot_base_url": "https://chatwoot.example.com",
                "chatwoot_account_id": "1",
                "chatwoot_inbox_id": "2",
                "chatwoot_api_token": "chatwoot-secret-token",
                "proxy_enabled": "1",
                "proxy_url": secret_proxy,
            },
        )
        self.assertEqual(response.status_code, 302)

        proxy = self.client.get("/internal/proxy")
        self.assertEqual(proxy.status_code, 200)
        self.assertEqual(proxy.json["proxy_url"], secret_proxy)

        admin = self.client.get("/admin")
        html = admin.get_data(as_text=True)
        self.assertNotIn("super-secret", html)
        self.assertNotIn("chatwoot-secret-token", html)
        self.assertIn("alice:***@proxy.example.com:8080", html)

    def test_disabled_proxy_returns_direct_mode(self):
        csrf = self.login()
        response = self.client.post(
            "/admin/settings",
            data={
                "csrf": csrf,
                "chatwoot_base_url": "https://chatwoot.example.com",
                "chatwoot_account_id": "1",
                "chatwoot_inbox_id": "2",
                "chatwoot_api_token": "token",
            },
        )
        self.assertEqual(response.status_code, 302)
        proxy = self.client.get("/internal/proxy")
        self.assertEqual(proxy.json, {"proxy_url": ""})

    def test_wrong_webhook_secret_is_hidden_as_404(self):
        response = self.client.post("/webhooks/chatwoot/wrong", json={"event": "message_created"})
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
