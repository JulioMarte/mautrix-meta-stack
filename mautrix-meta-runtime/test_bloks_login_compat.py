#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import pathlib
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "apply_bloks_login_compat", HERE / "apply_bloks_login_compat.py"
)
mod = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(mod)


class BloksLoginCompatPatchTests(unittest.TestCase):
    def source(self):
        return mod.OLD + "\n" + mod.ASSERT_TYPE_OLD + "\n" + mod.INTERP_BIND_OLD + "\n"

    def test_backports_upstream_i64_convert(self):
        patched = mod.patch_interp(self.source())
        self.assertIn('case "bk.action.i64.Convert":', patched)
        self.assertIn('case int64:', patched)
        self.assertIn('case float64:', patched)
        self.assertIn('BloksLiteralOf(int64(val))', patched)
        self.assertIn('can\'t convert %T to i64', patched)

    def test_backports_upstream_numeric_union_assertion(self):
        patched = mod.patch_interp(self.source())
        self.assertIn("if expected == 100 {", patched)
        self.assertIn("case 3, 4:", patched)
        self.assertIn("actual = expected", patched)
        self.assertNotIn(mod.ASSERT_TYPE_OLD, patched)

    def test_backports_interp_bind_args(self):
        patched = mod.patch_interp(self.source())
        self.assertIn("func InterpBindArgs", patched)
        self.assertIn("for i, arg := range args", patched)

    def test_backports_connector_cookie_submission(self):
        source = mod.CONNECTOR_COOKIES_OLD + "\n" + mod.CONNECTOR_INTERFACE_OLD
        patched = mod.patch_connector_login(source)
        self.assertIn("func (m *MetaNativeLogin) SubmitCookies", patched)
        self.assertIn("LoginProcessCookies", patched)

    def test_backports_webview_minify_metadata(self):
        patched = mod.patch_minify(mod.MINIFY_WEBVIEW_OLD)
        self.assertIn('"帍": "webview"', patched)
        self.assertIn('"3": "callback"', patched)
        self.assertIn('"4": "url"', patched)

    def test_backports_final_recaptcha_state_and_extract_contract(self):
        source = (
            mod.SELENIUM_FIND_OLD
            + "\n" + mod.SELENIUM_STATE_OLD
            + "\n" + mod.SELENIUM_ROUTE_OLD
            + "\n" + mod.SELENIUM_RECAPTCHA_ANCHOR
        )
        patched = mod.patch_selenium(source)
        self.assertIn("StateReCaptchaPage", patched)
        self.assertIn("FindDescendantIncludingEmbedded", patched)
        self.assertIn('StepID:       "fi.mau.meta.messengerlite.recaptcha"', patched)
        self.assertIn('Name: "recaptcha_token"', patched)
        self.assertIn('resolve({recaptcha_token: JSON.parse(data)["g-recaptcha-response"]})', patched)
        self.assertIn("InterpBindArgs(ctx, token)", patched)
        self.assertIn('Bool("has_recaptcha_token", token != "")', patched)
        self.assertNotIn('Str("recaptcha_token", token)', patched)
        self.assertNotIn(mod.SELENIUM_ROUTE_OLD, patched)

    def test_recaptcha_patchers_fail_closed_if_anchor_changes(self):
        with self.assertRaises(RuntimeError):
            mod.patch_connector_login(mod.CONNECTOR_INTERFACE_OLD)
        with self.assertRaises(RuntimeError):
            mod.patch_minify("not the pinned minify source")
        with self.assertRaises(RuntimeError):
            mod.patch_selenium(mod.SELENIUM_STATE_OLD)

    def test_patch_is_fail_closed_if_convert_anchor_changes(self):
        with self.assertRaises(RuntimeError):
            mod.patch_interp(mod.ASSERT_TYPE_OLD)

    def test_patch_is_fail_closed_if_assert_anchor_changes(self):
        with self.assertRaises(RuntimeError):
            mod.patch_interp(mod.OLD)

    def test_patch_refuses_double_application(self):
        once = mod.patch_interp(self.source())
        with self.assertRaises(RuntimeError):
            mod.patch_interp(once)

        connector_once = mod.patch_connector_login(mod.CONNECTOR_COOKIES_OLD + "\n" + mod.CONNECTOR_INTERFACE_OLD)
        with self.assertRaises(RuntimeError):
            mod.patch_connector_login(connector_once)

        minify_once = mod.patch_minify(mod.MINIFY_WEBVIEW_OLD)
        with self.assertRaises(RuntimeError):
            mod.patch_minify(minify_once)

        selenium_once = mod.patch_selenium(
            mod.SELENIUM_FIND_OLD + "\n" + mod.SELENIUM_STATE_OLD + "\n"
            + mod.SELENIUM_ROUTE_OLD + "\n" + mod.SELENIUM_RECAPTCHA_ANCHOR
        )
        with self.assertRaises(RuntimeError):
            mod.patch_selenium(selenium_once)


if __name__ == "__main__":
    unittest.main()
