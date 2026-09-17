import unittest

from meta_cookie_auth import CookieInputError, login_with_browser_cookies, parse_facebook_cookie_input


VALID = {
    "datr": "datr-value",
    "c_user": "123456789",
    "sb": "sb-value",
    "xs": "11:secret:2:1234567890:-1:-1",
}


class FakeClient:
    def __init__(self, fields=None):
        self.submitted = None
        self.fields = fields
        self.cancelled = []

    def start(self, flow_id):
        if flow_id != "facebook":
            raise AssertionError(flow_id)
        step = {
            "type": "cookies",
            "login_id": "login-1",
            "step_id": "fi.mau.meta.cookies",
            "txn_id": "txn-1",
        }
        if self.fields is not None:
            step["cookies"] = {"fields": self.fields}
        return step

    def submit_cookies_trusted(self, login_id, step_id, cookies, *, txn_id=""):
        self.submitted = (login_id, step_id, dict(cookies), txn_id)
        return {"type": "complete", "instructions": "Logged in"}

    def cancel(self, login_id):
        self.cancelled.append(login_id)


class MetaCookieAuthTests(unittest.TestCase):
    def test_accepts_copy_as_curl_cookie_header(self):
        raw = "curl 'https://www.facebook.com/api/graphql/' -H 'accept: */*' -H 'cookie: locale=en_US; datr=datr-value; c_user=123456789; sb=sb-value; xs=11:secret:2:1234567890:-1:-1; wd=1920x1080'"
        self.assertEqual(parse_facebook_cookie_input(raw), VALID)

    def test_accepts_json_object(self):
        raw = '{"datr":"datr-value","c_user":"123456789","sb":"sb-value","xs":"11:secret:2:1234567890:-1:-1","extra":"ignored"}'
        self.assertEqual(parse_facebook_cookie_input(raw), VALID)

    def test_accepts_browser_extension_json_array(self):
        raw = "[" + ",".join(
            f'{{"name":"{key}","value":"{value}"}}' for key, value in VALID.items()
        ) + "]"
        self.assertEqual(parse_facebook_cookie_input(raw), VALID)

    def test_rejects_incomplete_session(self):
        with self.assertRaisesRegex(CookieInputError, "xs"):
            parse_facebook_cookie_input("datr=a; c_user=b; sb=c")

    def test_cookie_login_preserves_legacy_four_cookie_contract_when_schema_missing(self):
        client = FakeClient()
        raw = "Cookie: locale=en_US; datr=datr-value; c_user=123456789; sb=sb-value; xs=11:secret:2:1234567890:-1:-1; wd=1"
        result = login_with_browser_cookies(client, raw)
        self.assertEqual(result["type"], "complete")
        self.assertEqual(
            client.submitted,
            ("login-1", "fi.mau.meta.cookies", VALID, "txn-1"),
        )
        self.assertEqual(client.cancelled, [])

    def test_cookie_login_follows_live_three_cookie_bridge_schema(self):
        fields = [
            {"id": "xs", "required": True},
            {"id": "c_user", "required": True},
            {"id": "datr", "required": True},
        ]
        client = FakeClient(fields)
        raw = "Cookie: locale=en_US; datr=datr-value; c_user=123456789; xs=11:secret:2:1234567890:-1:-1; wd=1"
        result = login_with_browser_cookies(client, raw)
        self.assertEqual(result["type"], "complete")
        self.assertEqual(
            client.submitted,
            (
                "login-1",
                "fi.mau.meta.cookies",
                {
                    "xs": "11:secret:2:1234567890:-1:-1",
                    "c_user": "123456789",
                    "datr": "datr-value",
                },
                "txn-1",
            ),
        )
        self.assertEqual(client.cancelled, [])

    def test_cookie_login_drops_supported_cookie_not_requested_by_bridge(self):
        fields = [
            {"id": "xs", "required": True},
            {"id": "c_user", "required": True},
            {"id": "datr", "required": True},
        ]
        client = FakeClient(fields)
        raw = "Cookie: datr=datr-value; c_user=123456789; sb=sb-value; xs=11:secret:2:1234567890:-1:-1"
        login_with_browser_cookies(client, raw)
        self.assertNotIn("sb", client.submitted[2])
        self.assertEqual(client.cancelled, [])

    def test_cookie_login_rejects_unknown_bridge_cookie_field_and_cancels_login(self):
        client = FakeClient([{"id": "future_cookie", "required": True}])
        with self.assertRaisesRegex(CookieInputError, "future_cookie"):
            login_with_browser_cookies(
                client,
                "Cookie: datr=datr-value; c_user=123456789; sb=sb-value; xs=11:secret:2:1234567890:-1:-1",
            )
        self.assertEqual(client.cancelled, ["login-1"])
        self.assertIsNone(client.submitted)

    def test_cookie_login_missing_live_required_cookie_cancels_login(self):
        fields = [
            {"id": "xs", "required": True},
            {"id": "c_user", "required": True},
            {"id": "datr", "required": True},
        ]
        client = FakeClient(fields)
        with self.assertRaisesRegex(CookieInputError, "datr"):
            login_with_browser_cookies(
                client,
                "Cookie: c_user=123456789; xs=11:secret:2:1234567890:-1:-1",
            )
        self.assertEqual(client.cancelled, ["login-1"])
        self.assertIsNone(client.submitted)


if __name__ == "__main__":
    unittest.main()
