# Meta onboarding implementation status

Status: cookie-first production path restored in `dev`; managed/helper flows retained for controlled testing; real-provider acceptance still required
Date: 2026-09-17
Contract: `managed-meta-onboarding.md`

## Current product path

The known-good browser-cookie workflow is the primary operator path:

```text
/admin
-> Facebook Messenger
-> /admin/meta-cookie
-> operator signs in normally at facebook.com
-> Copy as cURL / Cookie header / supported cookie JSON
-> integration extracts only datr, c_user, sb and xs
-> server-side provisioning adapter submits those cookies to private mautrix-meta BridgeV2
-> Connected
```

This path does not execute pasted cURL and does not persist raw cookies in generic integration settings or logs. It exists because this cookie method is already proven against the deployed Meta bridge and must not be removed while newer login approaches are still under evaluation.

## Experimental managed path

The newer managed onboarding remains available at `/admin/meta` and is exposed in the admin navigation as **Facebook login (prueba)**.

It continues to support:

- dynamic BridgeV2 login-flow discovery;
- Messenger Android/iOS user-input flows when exposed by the pinned bridge;
- ordinary `user_input` and `display_and_wait` steps;
- helper handoff for privileged web-cookie steps;
- one-time/replay-resistant helper pairing;
- sanitized onboarding state and diagnostics;
- logout/cancel operations.

The trusted Electron helper under `meta-auth-helper/` remains part of this experimental path. It uses an in-memory session partition, restricts navigation to approved Meta origins, reads only cookie names requested by mautrix, and posts them once to the integration backend.

## Private provisioning boundary

`integration/meta_provisioning.py` remains the server-side BridgeV2 adapter for both paths. The mautrix provisioning shared secret stays server-side and port 29319 remains container-network-only.

The adapter supports `whoami`, login-flow discovery, login start, `user_input`, `cookies`, `display_and_wait`, cancel and logout. Raw passwords, cookie values, WebAuthn assertions and provisioning credentials are not stored in the generic settings database.

## Production routing

`integration/meta_admin_patch.py` makes `/admin/meta-cookie` the primary **Facebook Messenger** navigation target.

`integration/runtime_entrypoint.py` explicitly imports `meta_cookie_page`, so the cookie-first page is registered in the production process.

`/admin/meta` is intentionally retained and reachable as the experimental managed/helper path. The previous compatibility redirect that forced `/admin/meta-cookie` to `/admin/meta` is no longer installed by the production entrypoint.

## Automated acceptance

The onboarding workflows must validate both paths rather than forcing one to replace the other:

- primary navigation targets `/admin/meta-cookie`;
- production runtime registers the cookie page;
- `/admin/meta-cookie` contains the cookie/cURL workflow;
- `/admin/meta` remains available for managed/helper testing;
- provisioning, helper expiry/replay/boundary, BridgeV2 input compatibility and managed E2E journey tests still run;
- the live stack still starts the exact Compose topology with `dock.mau.dev/mautrix/meta:v26.08.1` and exercises the real provisioning state machine.

## What automated CI still cannot prove

CI still cannot truthfully prove:

1. real Facebook authentication through every checkpoint/passkey/2FA/CAPTCHA variant;
2. signed/notarized desktop-helper installers on customer operating systems;
3. the external custom-protocol handoff on a deployed HTTPS domain;
4. a real Messenger/Marketplace round trip through Chatwoot using a newly created helper session;
5. persistence through an actual Coolify redeploy with a real Meta session.

## Required staging acceptance

For the current primary path:

```text
/admin/meta-cookie
-> obtain cookies from a real authenticated Facebook browser session
-> connect
-> mautrix reports complete
-> inbound Messenger/Marketplace conversation reaches Chatwoot
-> Chatwoot reply reaches Meta
-> redeploy/restart
-> session reconnects normally
```

Separately test `/admin/meta` and the desktop helper without making them authoritative until that real-provider acceptance succeeds.

## Promotion rule

Do not promote this feature to `main` merely because automated CI is green. Real-provider staging remains mandatory, and promotion from `dev` to `main` remains a separate human-controlled action under `development-branch-workflow.md`.
