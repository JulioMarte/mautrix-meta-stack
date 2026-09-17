# Managed Meta onboarding implementation status

Status: restored on `fix/restore-managed-meta-onboarding`; automated acceptance in progress; real-provider acceptance pending
Date: 2026-09-17
Contract: `managed-meta-onboarding.md`

## Current product path

The supported operator path is again the managed flow defined by the architecture contract:

```text
/admin
-> Facebook Messenger
-> /admin/meta
-> choose/start a BridgeV2 login flow
-> complete normal user-input steps in the panel
-> if the pinned bridge requests privileged cookies, open the trusted local helper
-> helper authenticates against Facebook in an isolated session
-> one-time handoff sends only the requested cookies to integration
-> integration submits them server-side to private mautrix provisioning
-> Connected
```

The short-lived `/admin/meta-cookie` flow that instructed the operator to open DevTools and paste `Copy as cURL` is no longer a supported product surface. Existing bookmarks to that path are redirected to `/admin/meta`; production `runtime_entrypoint.py` no longer imports/registers the manual cookie page.

This restores the non-negotiable product rule: a normal operator must not need Element, Matrix terminology, DevTools, cURL, raw cookies or the mautrix internal provisioning port.

## What is implemented

### Private provisioning adapter

`integration/meta_provisioning.py` provides the server-side BridgeV2 provisioning client for the pinned mautrix-meta runtime.

It:

- loads the provisioning secret only on the server-side trust boundary;
- sends it only as a private Bearer credential;
- supplies the configured Matrix admin identity as provisioning `user_id`;
- disables ambient process proxy inheritance for private provisioning calls;
- refuses redirects and applies bounded timeouts;
- supports `whoami`, login-flow discovery, login start, `user_input`, trusted `cookies`, `display_and_wait`, cancel and logout;
- normalizes connection state for the product UI;
- sanitizes step metadata before persistence/rendering.

Raw passwords, cookie values, WebAuthn assertions and provisioning credentials are not stored in the generic integration settings database.

### NiceGUI managed onboarding

`integration/nicegui_app.py` owns `/admin/meta` and remains the supported customer-facing Meta onboarding surface.

The page:

- is protected by the existing admin authentication;
- discovers login flows dynamically from the pinned bridge;
- prefers the Messenger mobile flows when the pinned runtime exposes them;
- starts/cancels login processes;
- supports ordinary `user_input` and `display_and_wait` steps;
- shows connected/connecting/action-required/disconnected product states;
- handles disconnect/logout;
- persists only sanitized transient step metadata;
- invokes the helper only when the actual BridgeV2 state machine returns a privileged `cookies` step.

`integration/meta_admin_patch.py` now routes the native Facebook Messenger navigation entry to `/admin/meta`.

### Trusted helper handoff

`integration/meta_helper_handoff.py` and `integration/meta_helper_routes.py` implement an ephemeral trusted handoff for BridgeV2 cookie steps:

- pairing ID plus cryptographically random bearer token;
- SHA-256 token digest only in process memory;
- five-minute expiry;
- single-use/replay-resistant semantics;
- no SQLite persistence of helper credentials;
- no mautrix provisioning secret in the desktop helper;
- safe GET descriptor containing only sanitized step metadata;
- bounded POST body and per-cookie size limits;
- allowlist of only cookie field IDs explicitly requested by mautrix;
- required-field validation before provisioning is called;
- raw cookie dictionaries cleared immediately after synchronous provisioning submission.

### Operational diagnostics

`integration/meta_onboarding_diagnostics.py` adds structured one-line JSON diagnostics under the logger name `meta_onboarding`.

The helper HTTP boundary emits correlation-friendly events such as:

- `helper_pairing_created`;
- `helper_descriptor_served` / `helper_descriptor_rejected`;
- `helper_submission_forwarding`;
- `helper_submission_rejected` with a safe reason;
- `helper_submission_failed` with normalized failure class/status;
- `helper_submission_complete`.

Diagnostics carry a bounded `trace_id` and operational identifiers but never intentionally include raw cookie values, Authorization headers, passwords, session tokens or provisioning secrets. The E2E journey asserts that known test secret values do not appear in captured onboarding logs.

The live-stack workflow prints these structured integration diagnostics and dumps full relevant container logs if a runtime gate fails.

### Electron helper

`meta-auth-helper/` contains the trusted local desktop client.

It:

- registers `mautrix-meta-helper://`;
- accepts only HTTPS integration origins, except localhost development;
- retrieves the safe one-time descriptor from integration;
- opens only the Meta URL returned by the bridge;
- uses an in-memory, non-persistent Electron partition;
- disables Node integration and renderer DevTools;
- denies browser permission requests and new windows;
- blocks navigation outside the allowed Meta origins;
- waits for the bridge-provided completion URL condition and the complete requested cookie set;
- reads only requested cookies through Electron's privileged cookie API;
- posts them once to the integration backend and closes the authentication window.

Electron and electron-builder are pinned. Packaging targets are configured for Windows NSIS, macOS DMG and Linux AppImage.

## Automated acceptance

`.github/workflows/meta-onboarding.yml` now treats the managed path as the contract rather than merely proving that some Meta page exists. It asserts that native navigation points to `/admin/meta`, fails if `/admin/meta-cookie` becomes the supported navigation target again, and runs:

- provisioning adapter tests;
- real HTTP provisioning contract tests;
- helper expiry/replay/boundary tests;
- BridgeV2 input compatibility tests;
- native admin managed-route tests;
- `test_meta_managed_onboarding_e2e.py`;
- existing NiceGUI regression tests;
- Electron helper syntax/security unit tests;
- production entrypoint import assertions that the manual cookie page is not registered.

The E2E journey covers the supported server/helper boundary from a BridgeV2 cookie step through pairing, descriptor retrieval, allowlisted cookie submission, completion persistence, structured diagnostics and replay rejection.

`.github/workflows/meta-onboarding-live.yml` now runs on normal `feature/**`, `fix/**` and `dev` development activity. It starts the exact Compose topology with `dock.mau.dev/mautrix/meta:v26.08.1`, verifies private secret isolation, exercises the real BridgeV2 provisioning state machine, confirms the production runtime exposes the managed route, and verifies port 29319 remains private.

## What automated CI still cannot prove

The following remain real-environment acceptance gaps rather than server-side implementation gaps:

1. **Real Facebook authentication in the packaged desktop helper.** Repository CI has no disposable Meta account and cannot truthfully exercise real provider checkpoints, passkeys, 2FA, CAPTCHA or risk challenges.
2. **Signed/notarized production installers.** Packaging targets exist, but customer distribution still requires platform build/signing/notarization credentials and release handling.
3. **External custom-protocol handoff on a deployed HTTPS domain.** The OS protocol registration, browser prompt, local helper, public integration origin and actual provider session must be exercised together.
4. **Real Messenger/Marketplace round trip after helper-created login.** This requires a live Meta account plus Chatwoot staging.
5. **Session persistence through a real Coolify restart/redeploy.** This must be observed against the actual named mautrix data volume after a real helper-created login.

## Required staging acceptance

On the exact `dev` candidate intended for release evaluation:

```text
/admin/meta
-> Connect Facebook
-> if requested, Open helper
-> complete real Facebook login / checkpoint / 2FA
-> helper sends one-time handoff
-> mautrix reports complete
-> /admin/meta shows the actual connected account
-> inbound Messenger/Marketplace conversation reaches Chatwoot without Element
-> Chatwoot reply reaches Meta
-> restart/redeploy the stack
-> session reconnects without repeating login in the normal case
-> reconnect from /admin/meta if required
-> disconnect from /admin/meta
```

During this pass, inspect the structured `meta_onboarding` events plus integration, mautrix-meta and Chatwoot logs for unexpected credential exposure or routing failures.

## Promotion rule

Do not promote this feature to `main` merely because automated CI is green. Real-provider staging remains mandatory, and promotion from `dev` to `main` remains a separate human-controlled action under `development-branch-workflow.md`.
