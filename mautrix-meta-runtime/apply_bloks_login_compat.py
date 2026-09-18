#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import subprocess
import sys

UPSTREAM_SHA = "001f276beca5b90dead1bbc1351e1036e3f966a7"

OLD = '''\tcase "bk.action.i64.Const":
\t\treturn i.Evaluate(ctx, &call.Args[0])
\tcase "bk.action.map.Get":
'''

NEW = '''\tcase "bk.action.i64.Const":
\t\treturn i.Evaluate(ctx, &call.Args[0])
\tcase "bk.action.i64.Convert":
\t\targ, err := i.Evaluate(ctx, &call.Args[0])
\t\tif err != nil {
\t\t\treturn nil, err
\t\t}
\t\tswitch val := arg.Value().(type) {
\t\tcase int64:
\t\t\treturn BloksLiteralOf(val), nil
\t\tcase float64:
\t\t\treturn BloksLiteralOf(int64(val)), nil
\t\t}
\t\treturn nil, fmt.Errorf("can't convert %T to i64", arg.Value())
\tcase "bk.action.map.Get":
'''

ASSERT_TYPE_OLD = '''\t\tactual, err := getBloksType(val)
\t\tif err != nil {
\t\t\treturn nil, err
\t\t}
\t\tif expected != actual {
\t\t\treturn nil, fmt.Errorf("bloks type assertion failure (%d != %d)", actual, expected)
\t\t}
'''

ASSERT_TYPE_NEW = '''\t\tactual, err := getBloksType(val)
\t\tif err != nil {
\t\t\treturn nil, err
\t\t}
\t\t// Native Bloks uses 100 as the numeric union type: either int or float.
\t\t// Newer Facebook 2FA payloads rely on this assertion.
\t\tif expected == 100 {
\t\t\tswitch actual {
\t\t\tcase 3, 4:
\t\t\t\tactual = expected
\t\t\t}
\t\t}
\t\tif expected != actual {
\t\t\treturn nil, fmt.Errorf("bloks type assertion failure (%d != %d)", actual, expected)
\t\t}
'''


INTERP_BIND_OLD = '''func InterpBindThis(ctx context.Context, this *BloksTreeComponent) context.Context {
	ambientArgs, ok := ctx.Value(interpCtxArgs).([]*BloksScriptLiteral)
	if !ok {
		ambientArgs = make([]*BloksScriptLiteral, maxInterpArgs)
	}
	ambientArgs[0] = BloksLiteralOf(&BloksElemRef{this})
	return context.WithValue(ctx, interpCtxArgs, ambientArgs)
}
'''

INTERP_BIND_NEW = '''func InterpBindThis(ctx context.Context, this *BloksTreeComponent) context.Context {
	return InterpBindArgs(ctx, &BloksElemRef{this})
}

func InterpBindArgs(ctx context.Context, args ...any) context.Context {
	ambientArgs, ok := ctx.Value(interpCtxArgs).([]*BloksScriptLiteral)
	if !ok {
		ambientArgs = make([]*BloksScriptLiteral, maxInterpArgs)
	}
	for i, arg := range args {
		ambientArgs[i] = BloksLiteralOf(arg)
	}
	return context.WithValue(ctx, interpCtxArgs, ambientArgs)
}
'''

CONNECTOR_COOKIES_OLD = '''func (m *MetaNativeLogin) SubmitUserInput(ctx context.Context, input map[string]string) (*bridgev2.LoginStep, error) {
	return m.proceed(ctx, input)
}

func (m *MetaNativeLogin) Wait(ctx context.Context) (*bridgev2.LoginStep, error) {
'''
CONNECTOR_COOKIES_NEW = '''func (m *MetaNativeLogin) SubmitUserInput(ctx context.Context, input map[string]string) (*bridgev2.LoginStep, error) {
	return m.proceed(ctx, input)
}

func (m *MetaNativeLogin) SubmitCookies(ctx context.Context, input map[string]string) (*bridgev2.LoginStep, error) {
	return m.proceed(ctx, input)
}

func (m *MetaNativeLogin) Wait(ctx context.Context) (*bridgev2.LoginStep, error) {
'''

CONNECTOR_INTERFACE_OLD = '''var _ bridgev2.LoginProcessUserInput = (*MetaNativeLogin)(nil)
var _ bridgev2.LoginProcessDisplayAndWait = (*MetaNativeLogin)(nil)
'''
CONNECTOR_INTERFACE_NEW = '''var _ bridgev2.LoginProcessUserInput = (*MetaNativeLogin)(nil)
var _ bridgev2.LoginProcessCookies = (*MetaNativeLogin)(nil)
var _ bridgev2.LoginProcessDisplayAndWait = (*MetaNativeLogin)(nil)
'''

MINIFY_WEBVIEW_OLD = '''    "㚮": "buttonExtension",
    "㚱": "accessibilityExtension"
  },
  "properties": {
    "bk.components.FoaTouchExtension": {
'''
MINIFY_WEBVIEW_NEW = '''    "㚮": "buttonExtension",
    "㚱": "accessibilityExtension",
    "帍": "webview"
  },
  "properties": {
    "webview": {
      "3": "callback",
      "4": "url"
    },
    "bk.components.FoaTouchExtension": {
'''

SELENIUM_FIND_OLD = '''func (bb *BloksBundle) FindDescendant(pred func(*BloksTreeComponent) bool) *BloksTreeComponent {
	return bb.Layout.Payload.Tree.FindDescendant(pred)
}

func (bb *BloksBundle) FindDescendants(pred func(*BloksTreeComponent) bool) []*BloksTreeComponent {
'''
SELENIUM_FIND_NEW = '''func (bb *BloksBundle) FindDescendant(pred func(*BloksTreeComponent) bool) *BloksTreeComponent {
	return bb.Layout.Payload.Tree.FindDescendant(pred)
}

func (bb *BloksBundle) FindDescendantIncludingEmbedded(pred func(*BloksTreeComponent) bool) *BloksTreeComponent {
	res := bb.Layout.Payload.Tree.FindDescendant(pred)
	if res != nil {
		return res
	}
	for _, embedded := range bb.Layout.Payload.Embedded {
		res := embedded.Contents.FindDescendant(pred)
		if res != nil {
			return res
		}
	}
	return nil
}

func (bb *BloksBundle) FindDescendants(pred func(*BloksTreeComponent) bool) []*BloksTreeComponent {
'''

SELENIUM_STATE_OLD = '''	StateCaptchaPage            BrowserState = "captcha-page"
	StateMFALandingPage         BrowserState = "mfa-landing-page"
'''
SELENIUM_STATE_NEW = '''	StateCaptchaPage            BrowserState = "captcha-page"
	StateReCaptchaPage          BrowserState = "recaptcha-page"
	StateMFALandingPage         BrowserState = "mfa-landing-page"
'''

SELENIUM_ROUTE_OLD = '''			case "com.bloks.www.two_step_verification.google_recaptcha":
				return ErrLoginReCaptcha
'''
SELENIUM_ROUTE_NEW = '''			case "com.bloks.www.two_step_verification.google_recaptcha":
				newState = StateReCaptchaPage
'''

SELENIUM_RECAPTCHA_ANCHOR = '''	case StateMFALandingPage:
'''
SELENIUM_RECAPTCHA_CASE = '''	case StateReCaptchaPage:
		webview := b.CurrentPage.FindDescendantIncludingEmbedded(FilterByComponent("webview"))
		if webview == nil {
			return nil, fmt.Errorf("can't find reCAPTCHA webview")
		}
		token := userInput["recaptcha_token"]
		if token == "" {
			url := webview.GetDynamicAttribute(ctx, b.CurrentPage.Interpreter, "url")
			if url == "" {
				return nil, fmt.Errorf("reCAPTCHA webview has no URL")
			}
			step = &bridgev2.LoginStep{
				Type:         bridgev2.LoginStepTypeCookies,
				StepID:       "fi.mau.meta.messengerlite.recaptcha",
				Instructions: "Complete the Google reCAPTCHA challenge.",
				CookiesParams: &bridgev2.LoginCookiesParams{
					URL: url,
					Fields: []bridgev2.LoginCookieField{{
						ID:       "recaptcha_token",
						Required: true,
						Sources:  []bridgev2.LoginCookieFieldSource{{Type: bridgev2.LoginCookieTypeSpecial, Name: "recaptcha_token"}},
					}},
					ExtractJS: `new Promise((resolve, reject) => {
						window.FbLoginRecaptcha = {
							onRecaptcha: data => {
								try {
									resolve({recaptcha_token: JSON.parse(data)["g-recaptcha-response"]});
								} catch (err) {
									reject(err);
								}
							}
						}
					})`,
				},
			}
			break
		}
		log.Debug().Str("recaptcha_token", token).Msg("Got recaptcha token from webview")
		callback := webview.GetScript("callback")
		if callback == nil {
			return nil, fmt.Errorf("reCAPTCHA webview has no callback")
		}
		delete(userInput, "recaptcha_token")
		if _, err = b.CurrentPage.Interpreter.Evaluate(InterpBindArgs(ctx, token), &callback.AST); err != nil {
			return nil, fmt.Errorf("submitting reCAPTCHA token: %w", err)
		}

	case StateMFALandingPage:
'''


def _replace_exact(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one upstream anchor, found {count}")
    return text.replace(old, new, 1)


def patch_connector_login(text: str) -> str:
    if "func (m *MetaNativeLogin) SubmitCookies" in text:
        raise RuntimeError("pkg/connector/login.go: reCAPTCHA cookies support is already implemented")
    text = _replace_exact(text, CONNECTOR_COOKIES_OLD, CONNECTOR_COOKIES_NEW, "pkg/connector/login.go SubmitCookies")
    return _replace_exact(text, CONNECTOR_INTERFACE_OLD, CONNECTOR_INTERFACE_NEW, "pkg/connector/login.go interface guard")


def patch_minify(text: str) -> str:
    if '"帍": "webview"' in text:
        raise RuntimeError("pkg/messagix/bloks/minify.json: webview metadata is already implemented")
    return _replace_exact(text, MINIFY_WEBVIEW_OLD, MINIFY_WEBVIEW_NEW, "pkg/messagix/bloks/minify.json webview")


def patch_selenium(text: str) -> str:
    if "StateReCaptchaPage" in text or 'StepID:       "fi.mau.meta.messengerlite.recaptcha"' in text:
        raise RuntimeError("pkg/messagix/bloks/selenium.go: reCAPTCHA support is already implemented")
    text = _replace_exact(text, SELENIUM_FIND_OLD, SELENIUM_FIND_NEW, "pkg/messagix/bloks/selenium.go embedded lookup")
    text = _replace_exact(text, SELENIUM_STATE_OLD, SELENIUM_STATE_NEW, "pkg/messagix/bloks/selenium.go state")
    text = _replace_exact(text, SELENIUM_ROUTE_OLD, SELENIUM_ROUTE_NEW, "pkg/messagix/bloks/selenium.go route")
    return _replace_exact(text, SELENIUM_RECAPTCHA_ANCHOR, SELENIUM_RECAPTCHA_CASE, "pkg/messagix/bloks/selenium.go step")



def patch_interp(text: str) -> str:
    convert_count = text.count(OLD)
    if convert_count != 1:
        raise RuntimeError(
            "pkg/messagix/bloks/interp.go: expected exactly one i64.Convert "
            f"upstream anchor, found {convert_count}"
        )
    if 'case "bk.action.i64.Convert":' in text:
        raise RuntimeError("pkg/messagix/bloks/interp.go: i64.Convert is already implemented")

    assert_count = text.count(ASSERT_TYPE_OLD)
    if assert_count != 1:
        raise RuntimeError(
            "pkg/messagix/bloks/interp.go: expected exactly one AssertType "
            f"upstream anchor, found {assert_count}"
        )
    if "if expected == 100 {" in text:
        raise RuntimeError(
            "pkg/messagix/bloks/interp.go: numeric AssertType compatibility is already implemented"
        )

    text = text.replace(OLD, NEW, 1).replace(ASSERT_TYPE_OLD, ASSERT_TYPE_NEW, 1)
    if "func InterpBindArgs" in text:
        raise RuntimeError("pkg/messagix/bloks/interp.go: InterpBindArgs is already implemented")
    return _replace_exact(text, INTERP_BIND_OLD, INTERP_BIND_NEW, "pkg/messagix/bloks/interp.go InterpBindArgs")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_bloks_login_compat.py <mautrix-meta-source-dir>")
    root = pathlib.Path(sys.argv[1]).resolve()
    actual_sha = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual_sha != UPSTREAM_SHA:
        raise SystemExit(
            f"unexpected mautrix-meta upstream SHA: {actual_sha}; expected {UPSTREAM_SHA}"
        )

    interp = root / "pkg/messagix/bloks/interp.go"
    connector_login = root / "pkg/connector/login.go"
    minify = root / "pkg/messagix/bloks/minify.json"
    selenium = root / "pkg/messagix/bloks/selenium.go"
    try:
        interp.write_text(patch_interp(interp.read_text()))
        connector_login.write_text(patch_connector_login(connector_login.read_text()))
        minify.write_text(patch_minify(minify.read_text()))
        selenium.write_text(patch_selenium(selenium.read_text()))
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
