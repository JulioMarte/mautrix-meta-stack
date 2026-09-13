import importlib
import os
import re
import tempfile
import unittest
from unittest.mock import patch


class ProductionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-production-tests"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"
        os.environ.pop("META_PROXY_URL", None)
        os.environ["META_PROXY_ENABLED"] = "false"
        global prod, legacy
        prod = importlib.import_module("prod_app")
        legacy = prod.legacy
        cls.client = prod.application.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        prod.ENV_PROXY_URL = ""
        prod.ENV_PROXY_ENABLED = False
        prod._portal_cache.clear()
        with legacy.db() as conn:
            conn.execute("DELETE FROM settings")
            conn.execute("DELETE FROM room_links")
            conn.execute("DELETE FROM processed_events")
        with self.client.session_transaction() as sess:
            sess.clear()

    def csrf_from(self, response):
        html = response.get_data(as_text=True)
        match = re.search(r'name=csrf value="([^"]+)"', html)
        self.assertIsNotNone(match)
        return match.group(1)

    def login(self):
        page = self.client.get("/admin/login")
        self.assertEqual(page.status_code, 200)
        csrf = self.csrf_from(page)
        response = self.client.post(
            "/admin/login",
            data={"csrf": csrf, "password": "admin-password-long-value"},
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as sess:
            return sess["csrf"]

    def configure_chatwoot(self):
        legacy.set_setting("chatwoot_base_url", "http://chatwoot.example.com")
        legacy.set_setting("chatwoot_account_id", "1")
        legacy.set_setting("chatwoot_inbox_id", "2")
        legacy.set_setting("chatwoot_api_token", "chatwoot-secret-token")

    def test_login_requires_csrf_and_admin_is_navigable(self):
        page = self.client.get("/admin/login")
        self.assertIn("Integration admin", page.get_data(as_text=True))
        no_csrf = self.client.post("/admin/login", data={"password": "admin-password-long-value"})
        self.assertEqual(no_csrf.status_code, 403)
        self.login()
        admin = self.client.get("/admin")
        html = admin.get_data(as_text=True)
        self.assertEqual(admin.status_code, 200)
        self.assertIn("Matrix ↔ Chatwoot", html)
        self.assertIn("Save configuration", html)
        self.assertIn("Test Chatwoot", html)

    def test_internal_proxy_resolver_is_not_public(self):
        self.assertEqual(self.client.get("/internal/proxy").status_code, 404)
        self.assertEqual(self.client.get("/internal/proxy/wrong-secret").status_code, 404)
        response = self.client.get("/internal/proxy/resolver-secret-long-value")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"proxy_url": ""})

    def test_coolify_proxy_overrides_panel_and_is_redacted(self):
        prod.ENV_PROXY_URL = "http://proxy-user:very-secret-password@proxy.example.com:8888"
        prod.ENV_PROXY_ENABLED = True
        resolver = self.client.get("/internal/proxy/resolver-secret-long-value")
        self.assertEqual(resolver.json["proxy_url"], prod.ENV_PROXY_URL)
        self.login()
        html = self.client.get("/admin").get_data(as_text=True)
        self.assertIn("managed by Coolify", html)
        self.assertIn("proxy-user:***@proxy.example.com:8888", html)
        self.assertNotIn("very-secret-password", html)

    def test_contact_uses_source_id_returned_by_chatwoot(self):
        self.configure_chatwoot()
        calls = []

        def fake_post(path, payload):
            calls.append((path, payload))
            if path.endswith("/contacts"):
                return {
                    "id": 10,
                    "identifier": "matrix-room:any",
                    "contact_inboxes": [{"inbox_id": 2, "source_id": "cw-existing-source"}],
                }
            if path.endswith("/conversations"):
                self.assertEqual(payload["source_id"], "cw-existing-source")
                return {"id": 55}
            self.fail(f"unexpected Chatwoot POST: {path}")

        with patch.object(legacy, "cw_post", side_effect=fake_post):
            link = prod.ensure_room_link("!portal:matrix.example.com", "@facebook_user:matrix.example.com")
        self.assertEqual(link["source_id"], "cw-existing-source")
        self.assertFalse(any("contact_inboxes" in path for path, _ in calls))

    def test_contact_creates_association_only_when_chatwoot_did_not_return_one(self):
        self.configure_chatwoot()

        def fake_post(path, payload):
            if path.endswith("/contacts"):
                return {"id": 11, "contact_inboxes": []}
            if "contact_inboxes" in path:
                return {"source_id": "cw-created-source"}
            if path.endswith("/conversations"):
                self.assertEqual(payload["source_id"], "cw-created-source")
                return {"id": 56}
            self.fail(f"unexpected Chatwoot POST: {path}")

        with patch.object(legacy, "cw_post", side_effect=fake_post):
            link = prod.ensure_room_link("!portal2:matrix.example.com", "@facebook_user:matrix.example.com")
        self.assertEqual(link["source_id"], "cw-created-source")

    def test_non_bridge_matrix_room_is_not_forwarded_to_chatwoot(self):
        self.configure_chatwoot()
        event = {
            "type": "m.room.message",
            "sender": "@someone:matrix.example.com",
            "event_id": "$event1",
            "content": {"msgtype": "m.text", "body": "should stay in Matrix"},
        }
        with patch.object(prod, "is_bridge_portal", return_value=False), patch.object(prod, "ensure_room_link") as ensure:
            prod.matrix_event_to_chatwoot("!normal:matrix.example.com", event)
        ensure.assert_not_called()
        self.assertFalse(legacy.event_seen("$event1"))

    def test_admin_save_keeps_coolify_managed_proxy_immutable(self):
        prod.ENV_PROXY_URL = "http://env-user:env-password@proxy.example.com:8888"
        prod.ENV_PROXY_ENABLED = True
        csrf = self.login()
        response = self.client.post(
            "/admin/settings",
            data={
                "csrf": csrf,
                "chatwoot_base_url": "http://chatwoot.example.com",
                "chatwoot_account_id": "1",
                "chatwoot_inbox_id": "2",
                "chatwoot_api_token": "token-value",
                "proxy_enabled": "1",
                "proxy_url": "http://attacker:bad@other.example.com:9999",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(prod.effective_proxy()[2], prod.ENV_PROXY_URL)
        self.assertEqual(legacy.get_setting("proxy_url"), "")

    def test_chatwoot_connectivity_test_requires_configured_inbox(self):
        self.configure_chatwoot()
        csrf = self.login()
        with patch.object(prod, "cw_get", return_value={"payload": [{"id": 2, "name": "Messenger"}]}):
            response = self.client.post("/admin/test-chatwoot", data={"csrf": csrf})
        self.assertEqual(response.status_code, 302)
        self.assertIn("Chatwoot+connection+verified", response.headers["Location"])


if __name__ == "__main__":
    unittest.main()
