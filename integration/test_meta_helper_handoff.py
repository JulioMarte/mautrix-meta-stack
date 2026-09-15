import unittest
from unittest.mock import patch

from meta_helper_handoff import HandoffError, HandoffRegistry


COOKIE_STEP = {
    "type": "cookies",
    "login_id": "process-1",
    "step_id": "fi.mau.meta.cookies",
    "txn_id": "txn-1",
    "instructions": "Login",
    "cookies": {
        "url": "https://www.facebook.com/",
        "fields": [
            {"id": "c_user", "required": True},
            {"id": "xs", "required": True},
        ],
    },
}


class HandoffRegistryTests(unittest.TestCase):
    def test_pairing_stores_digest_not_raw_token_and_is_one_time(self):
        registry = HandoffRegistry(ttl=300)
        item, token = registry.create(COOKIE_STEP)
        self.assertNotEqual(item.token_digest, token)
        self.assertNotIn(token, repr(item))
        fetched = registry.get(item.handoff_id, token)
        self.assertEqual(fetched.login_id, "process-1")
        consumed = registry.get(item.handoff_id, token, consume=True)
        self.assertIsNotNone(consumed.used_at)
        with self.assertRaisesRegex(HandoffError, "already used"):
            registry.get(item.handoff_id, token)

    def test_wrong_token_fails(self):
        registry = HandoffRegistry(ttl=300)
        item, _token = registry.create(COOKIE_STEP)
        with self.assertRaisesRegex(HandoffError, "Invalid"):
            registry.get(item.handoff_id, "wrong-token")

    def test_expired_pairing_fails_closed(self):
        registry = HandoffRegistry(ttl=5)
        with patch("meta_helper_handoff.time.time", return_value=1000):
            item, token = registry.create(COOKIE_STEP)
        with patch("meta_helper_handoff.time.time", return_value=1006):
            with self.assertRaisesRegex(HandoffError, "not found or expired|expired"):
                registry.get(item.handoff_id, token)

    def test_non_cookie_step_cannot_create_pairing(self):
        registry = HandoffRegistry()
        with self.assertRaisesRegex(HandoffError, "cookie"):
            registry.create({"type": "user_input", "login_id": "p", "step_id": "s"})


if __name__ == "__main__":
    unittest.main()
