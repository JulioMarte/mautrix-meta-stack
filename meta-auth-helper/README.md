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

A normal web page cannot read another origin's HttpOnly Facebook/Messenger cookies. The helper therefore runs an isolated Electron web session and uses Electron's privileged cookie API after the user finishes authenticating on the official Meta site.

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
- Navigation is limited to `facebook.com`, `messenger.com`, and their subdomains.
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
