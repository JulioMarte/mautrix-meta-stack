import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

import meta_helper_routes as routes
from meta_helper_handoff import HandoffRegistry


STEP = {
    "type": "cookies",
    "login_id": "process-1",
    "step_id": "fi.mau.meta.cookies",
    "txn_id": "txn-1",
    "instructions": "Login to Facebook",
    "cookies": {
        "url": "https://www.facebook.com/",
        "user_agent": "test-agent",
        "wait_for_url_pattern": "^https://www\\.facebook\\.com/",
        "fields": [
            {"id": "c_user", "required": True},
            {"id": "xs", "required": True},
        ],
    },
}


class FakeProvisioning:
    def __init__(self):
        self.calls = []
        self.fail = None

    def submit_cookies_trusted(self, login_id, step_id, cookies, *, txn_id=""):
        self.calls.append((login_id, step_id, dict(cookies), txn_id))
        if self.fail:
            raise self.fail
        return {"type": "complete", "step_id": "done", "instructions": "Logged in"}


class HelperRoutesTests(unittest.TestCase):
    def setUp(self):
        self.original_registry = routes.registry
        routes.registry = HandoffRegistry(ttl=300)
        self.fake = FakeProvisioning()
        self.stored = []
        app = FastAPI()
        routes.register_helper_routes(app, lambda: self.fake, self._store)
        self.client = TestClient(app)

    def tearDown(self):
        routes.registry = self.original_registry

    def _store(self, step):
        self.stored.append(dict(step))
        return dict(step)

    def pair(self):
        item, token = routes.registry.create(STEP)
        return item.handoff_id, token

    def headers(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_descriptor_requires_bearer_and_contains_no_secret_values(self):
        handoff_id, token = self.pair()
        denied = self.client.get(f"/api/meta/helper/{handoff_id}")
        self.assertEqual(denied.status_code, 401)
        response = self.client.get(f"/api/meta/helper/{handoff_id}", headers=self.headers(token))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["step"]["cookies"]["url"], "https://www.facebook.com/")
        self.assertNotIn(token, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["pragma"], "no-cache")

    def test_complete_cookie_submission_is_one_time(self):
        handoff_id, token = self.pair()
        payload = {"cookies": {"c_user": "123", "xs": "session", "unexpected": "drop-me"}}
        response = self.client.post(f"/api/meta/helper/{handoff_id}", headers=self.headers(token), json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["complete"])
        self.assertEqual(len(self.fake.calls), 1)
        login_id, step_id, cookies, txn_id = self.fake.calls[0]
        self.assertEqual((login_id, step_id, txn_id), ("process-1", "fi.mau.meta.cookies", "txn-1"))
        self.assertEqual(cookies, {"c_user": "123", "xs": "session"})
        replay = self.client.post(f"/api/meta/helper/{handoff_id}", headers=self.headers(token), json=payload)
        self.assertEqual(replay.status_code, 401)
        self.assertEqual(len(self.fake.calls), 1)

    def test_missing_required_cookie_can_be_retried_without_new_pairing(self):
        handoff_id, token = self.pair()
        response = self.client.post(
            f"/api/meta/helper/{handoff_id}",
            headers=self.headers(token),
            json={"cookies": {"c_user": "123"}},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("xs", response.json()["missing"])
        self.assertEqual(self.fake.calls, [])
        descriptor = self.client.get(f"/api/meta/helper/{handoff_id}", headers=self.headers(token))
        self.assertEqual(descriptor.status_code, 200)
        complete = self.client.post(
            f"/api/meta/helper/{handoff_id}",
            headers=self.headers(token),
            json={"cookies": {"c_user": "123", "xs": "session"}},
        )
        self.assertEqual(complete.status_code, 200)
        self.assertEqual(len(self.fake.calls), 1)

    def test_invalid_json_does_not_spend_pairing(self):
        handoff_id, token = self.pair()
        response = self.client.post(
            f"/api/meta/helper/{handoff_id}",
            headers={**self.headers(token), "Content-Type": "application/json"},
            content=b"{not-json",
        )
        self.assertEqual(response.status_code, 400)
        retry = self.client.get(f"/api/meta/helper/{handoff_id}", headers=self.headers(token))
        self.assertEqual(retry.status_code, 200)

    def test_wrong_bearer_never_reaches_provisioning(self):
        handoff_id, _token = self.pair()
        response = self.client.post(
            f"/api/meta/helper/{handoff_id}",
            headers=self.headers("wrong"),
            json={"cookies": {"c_user": "123", "xs": "session"}},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.fake.calls, [])

    def test_oversized_content_length_is_rejected_before_authentication(self):
        handoff_id, token = self.pair()
        response = self.client.post(
            f"/api/meta/helper/{handoff_id}",
            headers={**self.headers(token), "Content-Length": str(routes.MAX_BODY_BYTES + 1)},
            content=b"{}",
        )
        self.assertEqual(response.status_code, 413)
        retry = self.client.get(f"/api/meta/helper/{handoff_id}", headers=self.headers(token))
        self.assertEqual(retry.status_code, 200)

    def test_arbitrary_internal_exception_is_not_exposed(self):
        handoff_id, token = self.pair()
        self.fake.fail = RuntimeError("database-password=super-secret")
        response = self.client.post(
            f"/api/meta/helper/{handoff_id}",
            headers=self.headers(token),
            json={"cookies": {"c_user": "123", "xs": "session"}},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Meta authentication could not be completed")
        self.assertNotIn("super-secret", response.text)
        replay = self.client.get(f"/api/meta/helper/{handoff_id}", headers=self.headers(token))
        self.assertEqual(replay.status_code, 401)


if __name__ == "__main__":
    unittest.main()
