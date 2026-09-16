import unittest

from meta_cookie_auth import CookieInputError, login_with_browser_cookies, parse_facebook_cookie_input


VALID = {
    "datr": "datr-value",
    "c_user": "123456789",
    "sb": "sb-value",
    "xs": "11:secret:2:1234567890:-1:-1",
}


class FakeClient:
    def __init__(self):
        self.submitted = None

    def start(self, flow_id):
        if flow_id != "facebook":
            raise AssertionError(flow_id)
        return {
            "type": "cookies",
            "login_id": "login-1",
            "step_id": "fi.mau.meta.cookies",
            "txn_id": "txn-1",
        }

    def submit_cookies_trusted(self, login_id, step_id, cookies, *, txn_id=""):
        self.submitted = (login_id, step_id, dict(cookies), txn_id)
        return {"type": "complete", "instructions": "Logged in"}


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

    def test_cookie_login_uses_facebook_flow_and_only_required_cookies(self):
        client = FakeClient()
        raw = "Cookie: locale=en_US; datr=datr-value; c_user=123456789; sb=sb-value; xs=11:secret:2:1234567890:-1:-1; wd=1"
        result = login_with_browser_cookies(client, raw)
        self.assertEqual(result["type"], "complete")
        self.assertEqual(
            client.submitted,
            ("login-1", "fi.mau.meta.cookies", VALID, "txn-1"),
        )


if __name__ == "__main__":
    unittest.main()
