#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import subprocess
import sys

UPSTREAM_SHA = "001f276beca5b90dead1bbc1351e1036e3f966a7"

STATE_OLD = '''\tStateCaptchaPage            BrowserState = "captcha-page"
\tStateMFALandingPage         BrowserState = "mfa-landing-page"
'''
STATE_NEW = '''\tStateCaptchaPage            BrowserState = "captcha-page"
\tStateReCaptchaPage          BrowserState = "recaptcha-page"
\tStateMFALandingPage         BrowserState = "mfa-landing-page"
'''

SCREEN_OLD = '''\t\t\tcase "com.bloks.www.two_step_verification.no_op_captcha":
\t\t\t\tnewState = StateSilentCaptchaPage
\t\t\tcase "com.bloks.www.two_step_verification.google_recaptcha":
\t\t\t\treturn ErrLoginReCaptcha
'''
SCREEN_NEW = '''\t\t\tcase "com.bloks.www.two_step_verification.no_op_captcha":
\t\t\t\tnewState = StateSilentCaptchaPage
\t\t\tcase "com.bloks.www.two_step_verification.google_recaptcha":
\t\t\t\tnewState = StateReCaptchaPage
'''

STEP_ANCHOR = '''\tcase StateMFALandingPage:
'''

STEP_CASE = """\tcase StateReCaptchaPage:
\t\twebview := b.CurrentPage.FindDescendantIncludingEmbedded(FilterByComponent("webview"))
\t\tif webview == nil {
\t\t\treturn nil, fmt.Errorf("can\'t find reCAPTCHA webview")
\t\t}
\t\ttoken := userInput["recaptcha_token"]
\t\tif token == "" {
\t\t\turl := webview.GetDynamicAttribute(ctx, b.CurrentPage.Interpreter, "url")
\t\t\tif url == "" {
\t\t\t\treturn nil, fmt.Errorf("reCAPTCHA webview has no URL")
\t\t\t}
\t\t\tstep = &bridgev2.LoginStep{
\t\t\t\tType:         bridgev2.LoginStepTypeCookies,
\t\t\t\tStepID:       "fi.mau.meta.messengerlite.recaptcha",
\t\t\t\tInstructions: "Complete the Google reCAPTCHA challenge.",
\t\t\t\tCookiesParams: &bridgev2.LoginCookiesParams{
\t\t\t\t\tURL: url,
\t\t\t\t\tFields: []bridgev2.LoginCookieField{{
\t\t\t\t\t\tID:       "recaptcha_token",
\t\t\t\t\t\tRequired: true,
\t\t\t\t\t\tSources:  []bridgev2.LoginCookieFieldSource{{Type: bridgev2.LoginCookieTypeSpecial, Name: "recaptcha_token"}},
\t\t\t\t\t}},
\t\t\t\t\tExtractJS: `new Promise((resolve, reject) => {
\t\t\t\t\t\twindow.FbLoginRecaptcha = {
\t\t\t\t\t\t\tonRecaptcha: data => {
\t\t\t\t\t\t\t\ttry {
\t\t\t\t\t\t\t\t\tresolve({recaptcha_token: JSON.parse(data)["g-recaptcha-response"]});
\t\t\t\t\t\t\t\t} catch (err) {
\t\t\t\t\t\t\t\t\treject(err);
\t\t\t\t\t\t\t\t}
\t\t\t\t\t\t\t}
\t\t\t\t\t\t}
\t\t\t\t\t})`,
\t\t\t\t},
\t\t\t}
\t\t\tbreak
\t\t}
\t\tdelete(userInput, "recaptcha_token")
\t\tcallback := webview.GetScript("callback")
\t\tif callback == nil {
\t\t\treturn nil, fmt.Errorf("reCAPTCHA webview has no callback")
\t\t}
\t\tif _, err = b.CurrentPage.Interpreter.Evaluate(InterpBindArgs(ctx, token), &callback.AST); err != nil {
\t\t\treturn nil, fmt.Errorf("submitting reCAPTCHA token: %w", err)
\t\t}

"""

def patch_selenium(text: str) -> str:
    for old, label in (
        (STATE_OLD, "state"),
        (SCREEN_OLD, "screen dispatch"),
        (STEP_ANCHOR, "login step"),
    ):
        count = text.count(old)
        if count != 1:
            raise RuntimeError(
                f"pkg/messagix/bloks/selenium.go: expected exactly one reCAPTCHA {label} anchor, found {count}"
            )
    if "StateReCaptchaPage" in text or 'StepID:       "fi.mau.meta.messengerlite.recaptcha"' in text:
        raise RuntimeError("pkg/messagix/bloks/selenium.go: reCAPTCHA compatibility is already implemented")
    return (
        text.replace(STATE_OLD, STATE_NEW, 1)
        .replace(SCREEN_OLD, SCREEN_NEW, 1)
        .replace(STEP_ANCHOR, STEP_CASE + STEP_ANCHOR, 1)
    )

def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_recaptcha_compat.py <mautrix-meta-source-dir>")
    root = pathlib.Path(sys.argv[1]).resolve()
    actual_sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if actual_sha != UPSTREAM_SHA:
        raise SystemExit(f"unexpected mautrix-meta upstream SHA: {actual_sha}; expected {UPSTREAM_SHA}")
    path = root / "pkg/messagix/bloks/selenium.go"
    try:
        path.write_text(patch_selenium(path.read_text()))
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

if __name__ == "__main__":
    main()
