# Meta Connection Helper

This directory contains the trusted local helper used by the managed Meta onboarding flow when mautrix-meta returns a BridgeV2 `cookies` login step.

The normal customer flow is:

```text
/admin/meta
  -> Connect Facebook
  -> Open helper
  -> Facebook login/checkpoint/2FA in the helper window
  -> helper submits only the required cookies through a one-time pairing
  -> mautrix-meta validates the session
  -> /admin/meta shows the connected account
```

## Why this exists

A normal web page cannot read another origin's HttpOnly Facebook/Messenger cookies or extract the result of a cross-origin interactive reCAPTCHA. The helper therefore runs an isolated Electron web session and uses the browser context requested by BridgeV2 after the user finishes authenticating on the official Meta site.

For the current Messenger Lite BridgeV2 login state machine, CAPTCHA handling is split as follows:

- text CAPTCHA: the admin panel renders the image and audio attachments returned by mautrix-meta and sends the operator-entered `captcha_code`;
- Google reCAPTCHA: the helper opens the bridge-provided Meta/fbsbx webview, the operator solves it interactively, and the helper submits only the resulting `recaptcha_token`;
- no-op/silent CAPTCHA: mautrix-meta executes it internally and no operator UI is required.

This does not claim support for future CAPTCHA types that mautrix-meta itself does not recognize.

### Pinned runtime compatibility

The production runtime is intentionally pinned to mautrix/meta `001f276beca5b90dead1bbc1351e1036e3f966a7` (v26.08.1 lineage). That baseline already supports the image/audio CAPTCHA and silent/no-op CAPTCHA, but it rejects `com.bloks.www.two_step_verification.google_recaptcha` with `FI.MAU.META_GOOGLE_RECAPTCHA`.

`mautrix-meta-runtime/apply_bloks_login_compat.py` therefore backports the minimal final upstream reCAPTCHA contract onto that exact SHA. The backport includes the BridgeV2 cookie-step implementation, Bloks webview metadata, embedded webview lookup, `InterpBindArgs`, `StateReCaptchaPage`, and the corrected `ExtractJS` result shape `{recaptcha_token: ...}`. The transform is fail-closed against source drift and the patched Go sources are formatted and compiled in the runtime image build.

The implementation follows the corrected upstream sequence rather than the earlier speculative version: upstream first introduced the webview path, then changed it to the native `FbLoginRecaptcha.onRecaptcha` callback, and finally fixed `ExtractJS` to return a field map compatible with BridgeV2.

It does **not** contain the mautrix provisioning secret. The integration backend remains the only component allowed to call mautrix provisioning with that secret.

## Security model

- Pairing is initiated from an already authenticated `/admin/meta` session.
- The browser hands the helper a random pairing ID + random bearer token through the `mautrix-meta-helper://` protocol.
- The pairing expires after five minutes and can be submitted only once.
- Only a SHA-256 digest of the pairing token exists in server memory; the token is not written to SQLite.
- The helper accepts HTTPS integration origins (HTTP only for localhost development).
- The Meta BrowserWindow uses an in-memory Electron partition (no `persist:` prefix).
- Node integration and DevTools are disabled in the Meta renderer.
- Permission requests are denied.
- Main-frame navigation is restricted to HTTPS Meta authentication origins; the Messenger Lite reCAPTCHA flow additionally permits the bridge-defined `fbsbx.com/captcha/recaptcha/iframe/` challenge URL. Embedded third-party frames required by the provider remain governed by Chromium/Electron web security rather than this main-frame allowlist.
- The helper reads only cookie names requested by the current mautrix login step.
- Raw cookie values are sent directly to the one-time integration endpoint and are never persisted by the integration service.
- A failed/partial submission still spends the pairing token to prevent replay.

## Development

Use a currently supported Node.js version, then:

```bash
npm install
npm start
```

The helper registers the `mautrix-meta-helper://` URL scheme. In development, OS protocol registration behavior differs by platform; launching the helper first and then clicking **Open helper** from `/admin/meta` is the simplest test path.

The currently pinned Electron version is `44.3.0`. Keep it explicitly pinned and review Electron security releases when updating it.

## Packaging

```bash
npm install
npm run dist
```

`electron-builder` produces an NSIS installer on Windows, a DMG on macOS, and an AppImage on Linux when built on the corresponding platform.

Production distribution still requires normal platform signing/notarization infrastructure. Do not present unsigned development artifacts to end customers as trusted production installers.

## Real-provider acceptance

A successful source build is not enough. Before this helper is considered production-ready, test the exact packaged artifact with a disposable Facebook account against the deployed `dev` stack and prove:

1. the custom protocol opens the installed helper;
2. Facebook login, 2FA/checkpoints and navigation work inside the restricted webview;
3. required cookies are captured only after the completion URL and cookie set are both satisfied;
4. the one-time handoff reaches the integration backend;
5. mautrix completes the login;
6. `/admin/meta` shows the connected account;
7. Messenger/Marketplace inbound + reply works without Element;
8. restart/redeploy preserves the mautrix session;
9. raw Meta cookies do not appear in integration/mautrix/helper logs.


## External contract references

The helper/runtime CAPTCHA contract is intentionally checked against these upstream/official references:

- BridgeV2 login API: https://pkg.go.dev/maunium.net/go/mautrix/bridgev2 — `LoginCookiesParams`, `LoginCookieFieldSource`, `LoginCookieTypeSpecial`, and `LoginProcessCookies`.
- Electron security guidance: https://www.electronjs.org/docs/latest/tutorial/security — remote content must keep Node integration disabled, context isolation/sandboxing enabled, permissions constrained, new windows blocked, and navigation limited.
- Electron `webContents`: https://www.electronjs.org/docs/latest/api/web-contents — `executeJavaScript()` waits on returned Promises; `will-navigate` is main-frame-only while frame navigation has separate events.
- Google reCAPTCHA v2 display contract: https://developers.google.com/recaptcha/docs/display — successful completion yields a `g-recaptcha-response` token through the configured callback.
- Google reCAPTCHA verification semantics: https://developers.google.com/recaptcha/docs/verify — response tokens are short-lived and single-use, so the helper submits the result immediately rather than persisting it.
- mautrix/meta pinned runtime: https://github.com/mautrix/meta/commit/001f276beca5b90dead1bbc1351e1036e3f966a7.
- Corrected upstream reCAPTCHA sequence used for the backport:
  - speculative support: https://github.com/mautrix/meta/commit/f5e19be9c7adf44553712406ba77fc79040caea6
  - native webview callback handling: https://github.com/mautrix/meta/commit/27814d1a505f6acc760cded86dcd3f812dd2d8f7
  - corrected `ExtractJS` field-map return: https://github.com/mautrix/meta/commit/03997a531030db363f70e6859150ff6f5c9ff424

