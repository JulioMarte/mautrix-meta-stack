import base64
import importlib
import os
import tempfile
import time
import unittest
from unittest.mock import patch


class FinalRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-final-tests"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"
        global final, legacy
        final = importlib.import_module("final_app")
        legacy = final.legacy
        cls.client = final.application.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        with legacy.db() as conn:
            conn.execute("DELETE FROM settings")
            conn.execute("DELETE FROM room_links")
            conn.execute("DELETE FROM processed_events")

    def basic_header(self, password):
        encoded = base64.b64encode(("mautrix:" + password).encode()).decode()
        return {"Authorization": "Basic " + encoded}

    def test_proxy_resolver_requires_internal_basic_auth(self):
        unauth = self.client.get("/internal/proxy")
        self.assertEqual(unauth.status_code, 404)
        wrong = self.client.get("/internal/proxy", headers=self.basic_header("wrong-secret-long-value"))
        self.assertEqual(wrong.status_code, 404)
        good = self.client.get("/internal/proxy", headers=self.basic_header("resolver-secret-long-value"))
        self.assertEqual(good.status_code, 200)
        self.assertEqual(good.json, {"proxy_url": ""})

    def test_transitional_secret_path_is_disabled(self):
        response = self.client.get("/internal/proxy/resolver-secret-long-value")
        self.assertEqual(response.status_code, 404)

    def test_historical_event_before_activation_is_dropped(self):
        cutoff = int(time.time() * 1000)
        legacy.set_setting("chatwoot_enabled_at_ms", str(cutoff))
        old_event = {"origin_server_ts": cutoff - 60_000, "event_id": "$old"}
        with patch.object(final, "base_matrix_event_to_chatwoot") as base:
            final.guarded_matrix_event_to_chatwoot("!portal:matrix.example.com", old_event)
        base.assert_not_called()

    def test_event_after_activation_is_forwarded_to_normal_filter_chain(self):
        cutoff = int(time.time() * 1000)
        legacy.set_setting("chatwoot_enabled_at_ms", str(cutoff))
        fresh_event = {"origin_server_ts": cutoff + 1, "event_id": "$fresh"}
        with patch.object(final, "base_matrix_event_to_chatwoot") as base:
            final.guarded_matrix_event_to_chatwoot("!portal:matrix.example.com", fresh_event)
        base.assert_called_once_with("!portal:matrix.example.com", fresh_event)

    def test_activation_boundary_is_persisted_once(self):
        legacy.set_setting("chatwoot_base_url", "http://chatwoot.example.com")
        legacy.set_setting("chatwoot_account_id", "1")
        legacy.set_setting("chatwoot_inbox_id", "2")
        legacy.set_setting("chatwoot_api_token", "token")
        final.ensure_activation_boundary()
        first = legacy.get_setting("chatwoot_enabled_at_ms")
        self.assertTrue(first.isdigit())
        time.sleep(0.01)
        final.ensure_activation_boundary()
        self.assertEqual(legacy.get_setting("chatwoot_enabled_at_ms"), first)


if __name__ == "__main__":
    unittest.main()
