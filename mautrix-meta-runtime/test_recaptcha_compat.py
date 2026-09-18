#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import pathlib
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "apply_recaptcha_compat", HERE / "apply_recaptcha_compat.py"
)
mod = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(mod)


class ReCaptchaCompatPatchTests(unittest.TestCase):
    def source(self):
        return mod.STATE_OLD + "\n" + mod.SCREEN_OLD + "\n" + mod.STEP_ANCHOR + "\n"

    def test_backports_interactive_recaptcha_state_and_bridge_step(self):
        patched = mod.patch_selenium(self.source())
        self.assertIn('StateReCaptchaPage          BrowserState = "recaptcha-page"', patched)
        self.assertIn('newState = StateReCaptchaPage', patched)
        self.assertIn('StepID:       "fi.mau.meta.messengerlite.recaptcha"', patched)
        self.assertIn('ID:       "recaptcha_token"', patched)
        self.assertIn('LoginCookieTypeSpecial', patched)
        self.assertIn('g-recaptcha-response', patched)
        self.assertIn('submitting reCAPTCHA token', patched)

    def test_patch_removes_old_unsupported_recaptcha_exit(self):
        patched = mod.patch_selenium(self.source())
        self.assertNotIn(mod.SCREEN_OLD, patched)
        self.assertNotIn('return ErrLoginReCaptcha', patched)

    def test_patch_fails_closed_if_any_anchor_changes(self):
        for source in (
            mod.SCREEN_OLD + "\n" + mod.STEP_ANCHOR,
            mod.STATE_OLD + "\n" + mod.STEP_ANCHOR,
            mod.STATE_OLD + "\n" + mod.SCREEN_OLD,
        ):
            with self.subTest(source=source[:30]), self.assertRaises(RuntimeError):
                mod.patch_selenium(source)

    def test_patch_refuses_double_application(self):
        once = mod.patch_selenium(self.source())
        with self.assertRaises(RuntimeError):
            mod.patch_selenium(once)


if __name__ == "__main__":
    unittest.main()
