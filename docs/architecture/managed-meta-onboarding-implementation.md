# Managed Meta onboarding implementation status

Status: implemented in `dev`, real-provider acceptance pending
Date: 2026-09-15
Contract: `managed-meta-onboarding.md`

## What is implemented

The current `dev` branch now implements the architecture described by the managed onboarding contract without exposing Element or the mautrix provisioning port to the customer.

### Private provisioning adapter

`integration/meta_provisioning.py` provides a server-side BridgeV2 provisioning client for the pinned mautrix-meta runtime.

It:

- reads `provisioning.shared_secret` from the existing read-only `/mautrix/config.yaml` mount (with an explicit env override only for tests/special deployments);
- sends the secret only as a server-side Bearer credential;
- supplies `MATRIX_ADMIN_MXID` as the provisioning `user_id`;
- disables ambient process proxy inheritance for these private calls;
- refuses redirects;
- applies bounded request timeouts;
- supports `whoami`, login-flow discovery, login start, `user_input`, trusted `cookies`, `display_and_wait`, cancel and logout;
- normalizes bridge state into product-facing connected/connecting/action-required/disconnected states;
- sanitizes login-step metadata before it is persisted/rendered.

Raw passwords, cookie values, WebAuthn assertions and provisioning credentials are not stored in the generic integration settings database.

### NiceGUI product surface

`integration/nicegui_app.py` adds `/admin/meta` while retaining the previously tested admin implementation in `integration/nicegui_legacy.py`.

The new surface:

- is protected by the existing admin authentication;
- discovers login flows dynamically from the pinned bridge;
- starts/cancels logins;
- renders product-facing connection state;
- supports normal `user_input` and `display_and_wait` steps;
- handles logout/disconnect;
- preserves only sanitized transient step metadata;
- exposes a visible `Conectar Facebook` entry from the existing `/admin` surface;
- never asks the operator to open Element or copy cookies from DevTools.

### Trusted-helper handoff

The current Facebook/Messenger flows in mautrix-meta v26.08.1 start with a BridgeV2 `cookies` step. Because browser JavaScript cannot read Facebook's HttpOnly cross-origin cookies, the stack now has an explicit local-helper protocol instead of an iframe/popup workaround.

`integration/meta_helper_handoff.py` and `integration/meta_helper_routes.py` implement an ephemeral handoff:

- pairing ID + cryptographically random bearer token;
- SHA-256 token digest only in process memory;
- five-minute expiry;
- single-use semantics;
- no SQLite persistence;
- no mautrix provisioning secret in the helper;
- safe GET descriptor for the required cookie step;
- bounded POST payload;
- allowlist of only the cookie field IDs requested by mautrix;
- required-cookie validation before provisioning is called;
- replay rejection even after a partial/failed submission;
- immediate clearing of local raw-cookie dictionaries after submission.

### Electron helper source

`meta-auth-helper/` contains the desktop helper implementation.

It:

- registers the `mautrix-meta-helper://` custom protocol;
- accepts only HTTPS integration origins (HTTP is allowed only for localhost development);
- fetches the current safe handoff descriptor with the one-time bearer;
- opens the exact Facebook/Messenger URL returned by mautrix;
- uses a non-persistent Electron session partition;
- disables Node integration and DevTools in the Meta renderer;
- denies browser permission requests;
- blocks navigation outside Facebook/Messenger domains;
- waits for both the mautrix completion URL pattern and the complete required cookie set;
- reads only the requested cookie names through Electron's privileged cookie API;
- posts those values once to the integration backend;
- closes the Meta window after success/failure and tells the user to return to `/admin`.

Electron is explicitly pinned in `package.json`; packaging is configured for Windows NSIS, macOS DMG and Linux AppImage.

## Automated evidence

`.github/workflows/meta-onboarding.yml` is a dedicated acceptance lane for this feature. It builds the integration image and runs:

- provisioning adapter tests;
- pairing expiry/replay tests;
- helper HTTP-boundary tests;
- full NiceGUI onboarding import;
- the pre-existing NiceGUI regression suite;
- JavaScript syntax validation for the Electron helper.

The repository-wide `Validate stack` workflow still runs independently and remains the regression gate for the Compose topology and existing transport behavior.

## What is not yet proven

The code path is implemented, but the following cannot be truthfully marked complete from repository CI alone:

1. **Real Facebook login through the packaged helper.** CI has no disposable Meta account and cannot exercise Facebook checkpoints/2FA.
2. **Packaged desktop artifact behavior.** Source and packaging configuration exist, but production installers still need platform build jobs plus signing/notarization credentials before customer distribution.
3. **End-to-end deployed handoff.** The custom URL protocol, public integration HTTPS domain, local helper, private provisioning API and real Meta session must be exercised together on the deployed `dev` revision.
4. **Real Messenger/Marketplace round trip after helper login.** This requires the existing staging checklist with a live account and Chatwoot.
5. **Session persistence across a real Coolify redeploy after helper-created login.** This must be observed with the actual named mautrix volume.

These are staging/release-evidence gaps, not missing server-side onboarding logic.

## Required next acceptance pass

On a deployed `dev` revision:

```text
/admin/meta
-> Connect Facebook
-> Open helper
-> complete Facebook login / checkpoint / 2FA
-> helper sends one-time cookie handoff
-> mautrix reports complete
-> /admin/meta shows connected account
-> inbound Messenger/Marketplace conversation reaches Chatwoot
-> Chatwoot reply reaches Meta
-> restart/redeploy
-> session reconnects without repeating login
-> disconnect from /admin/meta
```

During that pass inspect integration, mautrix and helper logs for accidental cookie/token exposure. None is expected by design.

## Promotion rule

Do not promote this feature to `main` merely because CI is green. The managed onboarding contract explicitly requires real-provider acceptance. Promotion remains a separate human decision after the exact `dev` candidate passes the staging flow above.
