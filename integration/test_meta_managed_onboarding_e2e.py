import importlib
import json
import os
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

import meta_helper_routes as routes
from meta_helper_handoff import HandoffRegistry


COOKIE_STEP = {
    "type": "cookies",
    "login_id": "login-e2e-1",
    "step_id": "fi.mau.meta.cookies",
    "txn_id": "txn-e2e-1",
    "instructions": "Authenticate with Facebook",
    "cookies": {
        "url": "https://www.facebook.com/",
        "wait_for_url_pattern": "^https://www\\.facebook\\.com/",
        "fields": [
            {"id": "datr", "required": True},
            {"id": "c_user", "required": True},
            {"id": "sb", "required": True},
            {"id": "xs", "required": True},
        ],
    },
}


class FakeBridgeV2:
    def __init__(self):
        self.received = []

    def submit_cookies_trusted(self, login_id, step_id, cookies, *, txn_id=""):
        self.received.append({
            "login_id": login_id,
            "step_id": step_id,
            "cookie_names": sorted(cookies),
            "txn_id": txn_id,
        })
        return {
            "type": "complete",
            "login_id": login_id,
            "step_id": "complete",
            "instructions": "Connected",
        }


class ManagedMetaOnboardingJourneyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-managed-e2e"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["MAUTRIX_PROVISIONING_SECRET"] = "provisioning-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"
        cls.patch = importlib.import_module("meta_admin_patch")
        cls.managed = importlib.import_module("nicegui_app")
        cls.cookie_page = importlib.import_module("meta_cookie_page")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.original_registry = routes.registry
        routes.registry = HandoffRegistry(ttl=300)
        self.bridge = FakeBridgeV2()
        self.stored = []
        app = FastAPI()
        routes.register_helper_routes(app, lambda: self.bridge, self._store)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        routes.registry = self.original_registry

    def _store(self, step):
        self.stored.append(dict(step))
        return dict(step)

    def test_persisted_login_failure_is_secret_safe_and_traceable(self):
        sensitive = "do-not-persist-this-value"
        exc = self.managed.ProvisioningError(
            f"provider rejected sensitive value {sensitive}",
            errcode="M_FORBIDDEN",
            status_code=400,
            trace_id="attempt-safe-001",
        )
        self.managed._record_meta_failure(exc, operation="submit_user_input")
        stored = self.managed._legacy_ui.legacy.get_setting(self.managed.META_LAST_FAILURE_KEY)
        self.assertNotIn(sensitive, stored)
        payload = json.loads(stored)
        self.assertEqual(payload["trace_id"], "attempt-safe-001")
        self.assertEqual(payload["code"], "META_LOGIN_REJECTED")
        self.assertEqual(payload["operation"], "submit_user_input")
        self.assertEqual(payload["status_code"], 400)

    def test_cookie_first_product_path_and_managed_helper_fallback_complete_end_to_end(self):
        # The managed BridgeV2 flow is now the supported production path after
        # real-provider validation. Cookie extraction remains an explicit fallback.
        self.assertIn(("meta", "Facebook Messenger", "forum", "/admin/meta"), self.patch.NAV_ITEMS)
        self.assertIn(("meta_fallback", "Facebook web fallback", "cookie", "/admin/meta-cookie"), self.patch.NAV_ITEMS)

        cookie_source = importlib.import_module("inspect").getsource(self.cookie_page.meta_cookie_page)
        self.assertIn("Copy as cURL", cookie_source)
        self.assertIn("login_with_browser_cookies", cookie_source)
        self.assertIn("Conectar Facebook", cookie_source)

        managed_source = importlib.import_module("inspect").getsource(self.managed.meta_onboarding_page)
        self.assertIn("create_pairing(saved_step)", managed_source)
        self.assertNotIn("Copy as cURL", managed_source)
        self.assertNotIn("Network / Red", managed_source)

        # Simulate the exact server/helper boundary for the experimental managed
        # path: desktop helper fetches sanitized metadata, captures only requested
        # cookies and posts them once, then BridgeV2 returns complete.
        item, token = routes.registry.create(COOKIE_STEP)
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Meta-Trace-Id": "journey-e2e-001",
        }
        descriptor = self.client.get(f"/api/meta/helper/{item.handoff_id}", headers=headers)
        self.assertEqual(descriptor.status_code, 200, descriptor.text)
        body = descriptor.json()
        self.assertEqual(body["step"]["type"], "cookies")
        self.assertEqual(body["trace_id"], "journey-e2e-001")
        self.assertNotIn(token, descriptor.text)

        raw_values = {
            "datr": "datr-secret-e2e",
            "c_user": "123456",
            "sb": "sb-secret-e2e",
            "xs": "xs-secret-e2e",
            "not_requested": "must-be-dropped",
        }
        with self.assertLogs("meta_onboarding", level="INFO") as captured:
            completed = self.client.post(
                f"/api/meta/helper/{item.handoff_id}",
                headers=headers,
                json={"cookies": raw_values},
            )
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertTrue(completed.json()["complete"])
        self.assertEqual(completed.json()["trace_id"], "journey-e2e-001")
        self.assertEqual(len(self.bridge.received), 1)
        self.assertEqual(
            self.bridge.received[0]["cookie_names"],
            ["c_user", "datr", "sb", "xs"],
        )
        self.assertEqual(self.stored[-1]["type"], "complete")

        joined_logs = "\n".join(captured.output)
        for secret in raw_values.values():
            self.assertNotIn(secret, joined_logs)
        self.assertIn("helper_submission_forwarding", joined_logs)
        self.assertIn("helper_submission_complete", joined_logs)
        for line in captured.output:
            json.loads(line.split(":", 2)[-1])

        replay = self.client.post(
            f"/api/meta/helper/{item.handoff_id}",
            headers=headers,
            json={"cookies": raw_values},
        )
        self.assertEqual(replay.status_code, 401)
        self.assertEqual(len(self.bridge.received), 1)


if __name__ == "__main__":
    unittest.main()
