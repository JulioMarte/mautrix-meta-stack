import unittest

from meta_cookie_input import CookieInputError, parse_cookie_input, select_required_cookies


REQUIRED = ["datr", "c_user", "sb", "xs"]


class MetaCookieInputTests(unittest.TestCase):
    def test_plain_cookie_header(self):
        parsed = parse_cookie_input("datr=D; c_user=123; sb=S; xs=X; extra=ignored")
        self.assertEqual(select_required_cookies(parsed, REQUIRED), {
            "datr": "D", "c_user": "123", "sb": "S", "xs": "X"
        })

    def test_cookie_header_prefix(self):
        parsed = parse_cookie_input("Cookie: datr=D; c_user=123; sb=S; xs=X")
        self.assertEqual(select_required_cookies(parsed, REQUIRED)["c_user"], "123")

    def test_json_object(self):
        parsed = parse_cookie_input('{"datr":"D","c_user":"123","sb":"S","xs":"X"}')
        self.assertEqual(set(select_required_cookies(parsed, REQUIRED)), set(REQUIRED))

    def test_json_cookie_extension_array(self):
        parsed = parse_cookie_input('[{"name":"datr","value":"D"},{"name":"c_user","value":"123"},{"name":"sb","value":"S"},{"name":"xs","value":"X"}]')
        self.assertEqual(select_required_cookies(parsed, REQUIRED)["xs"], "X")

    def test_curl_cookie_flag(self):
        parsed = parse_cookie_input("curl 'https://www.facebook.com/api/graphql/' -b 'datr=D; c_user=123; sb=S; xs=X'")
        self.assertEqual(select_required_cookies(parsed, REQUIRED)["sb"], "S")

    def test_curl_cookie_header(self):
        parsed = parse_cookie_input("curl 'https://www.facebook.com/api/graphql/' -H 'cookie: datr=D; c_user=123; sb=S; xs=X'")
        self.assertEqual(select_required_cookies(parsed, REQUIRED)["datr"], "D")

    def test_missing_required_cookie_is_explicit(self):
        parsed = parse_cookie_input("datr=D; c_user=123; sb=S")
        with self.assertRaisesRegex(CookieInputError, "xs"):
            select_required_cookies(parsed, REQUIRED)

    def test_empty_input_rejected(self):
        with self.assertRaises(CookieInputError):
            parse_cookie_input("  ")


if __name__ == "__main__":
    unittest.main()
