import importlib
import os
import tempfile
import unittest
from unittest.mock import Mock, patch


class RuntimeMetaRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-route-tests"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"
        os.environ.pop("META_PROXY_URL", None)
        os.environ["META_PROXY_ENABLED"] = "false"
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

    def _session(self, ip):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"ip": ip}
        session = Mock()
        session.trust_env = True
        session.get.return_value = response
        return session

    def test_resolver_payload_is_direct_when_toggle_is_off_even_with_saved_proxy(self):
        legacy.set_setting("proxy_enabled", "0")
        legacy.set_setting("proxy_url", "http://user:pass@proxy.example.com:8888")
        self.assertEqual(module.proxy_resolver_payload(), {"proxy_url": ""})

    def test_resolver_payload_returns_saved_proxy_only_when_toggle_is_on(self):
        proxy = "http://user:pass@proxy.example.com:8888"
        legacy.set_setting("proxy_enabled", "1")
        legacy.set_setting("proxy_url", proxy)
        self.assertEqual(module.proxy_resolver_payload(), {"proxy_url": proxy})

    def test_runtime_route_test_uses_direct_path_when_resolver_is_direct(self):
        legacy.set_setting("proxy_enabled", "0")
        legacy.set_setting("proxy_url", "http://user:pass@proxy.example.com:8888")
        direct = self._session("198.51.100.20")
        with patch.object(module.requests, "Session", return_value=direct):
            result = module.verify_meta_route()
        self.assertEqual(result["mode"], "DIRECT")
        self.assertEqual(result["route_ip"], "198.51.100.20")
        self.assertEqual(legacy.get_setting("proxy_verified_mode"), "DIRECT")
        self.assertEqual(legacy.get_setting("proxy_verified_ip"), "")
        direct.get.assert_called_once_with(module.IP_CHECK_URL, proxies=None, timeout=20)

    def test_runtime_route_test_uses_exact_saved_proxy_when_resolver_is_proxy(self):
        proxy = "http://user:pass@proxy.example.com:8888"
        legacy.set_setting("proxy_enabled", "1")
        legacy.set_setting("proxy_url", proxy)
        direct = self._session("198.51.100.20")
        proxied = self._session("203.0.113.10")
        with patch.object(module.requests, "Session", side_effect=[direct, proxied]):
            result = module.verify_meta_route()
        self.assertEqual(result["mode"], "PROXY")
        self.assertTrue(result["different"])
        self.assertEqual(result["route_ip"], "203.0.113.10")
        self.assertEqual(legacy.get_setting("proxy_verified_mode"), "PROXY")
        self.assertEqual(legacy.get_setting("proxy_verified_ip"), "203.0.113.10")
        self.assertEqual(proxied.get.call_args.kwargs["proxies"], {"http": proxy, "https": proxy})

    def test_route_change_invalidates_previous_route_verification(self):
        legacy.set_setting("chatwoot_base_url", "http://chatwoot.example.com")
        legacy.set_setting("chatwoot_account_id", "1")
        legacy.set_setting("chatwoot_inbox_id", "2")
        legacy.set_setting("chatwoot_api_token", "token")
        legacy.set_setting("proxy_url", "http://user:pass@proxy.example.com:8888")
        legacy.set_setting("proxy_enabled", "1")
        legacy.set_setting("proxy_verified_at", "old")
        legacy.set_setting("proxy_verified_ip", "203.0.113.10")
        legacy.set_setting("proxy_verified_mode", "PROXY")
        module.save_configuration(
            "http://chatwoot.example.com", "1", "2", "", False,
            "http://user:pass@proxy.example.com:8888",
        )
        self.assertEqual(legacy.get_setting("proxy_enabled"), "0")
        self.assertEqual(legacy.get_setting("proxy_verified_at"), "")
        self.assertEqual(legacy.get_setting("proxy_verified_ip"), "")
        self.assertEqual(legacy.get_setting("proxy_verified_mode"), "")


if __name__ == "__main__":
    unittest.main()
