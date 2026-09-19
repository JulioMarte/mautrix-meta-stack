# Managed Meta onboarding for the single-client stack

Status: implemented production path; live deployment acceptance still required
Date: 2026-09-15
Applies to: current single-client `main` deployment line

## Decision

The customer-facing Facebook Messenger / Marketplace onboarding flow belongs in the existing NiceGUI admin surface exposed by the `integration` service. Matrix, Synapse, mautrix-meta, bridge management rooms, Element, appservice registration, and bridge-internal identifiers are infrastructure and must not appear in the normal customer workflow.

The production experience is:

```text
/admin/meta
  -> Facebook Messenger
  -> Messenger Android (recommended)
  -> complete the BridgeV2 user_input steps in the authenticated admin
  -> Connected
  -> Messenger / Marketplace sync starts
```

The browser-cookie flow at `/admin/meta-cookie` is retained as a recovery fallback only. It is not the normal customer onboarding path.

The normal user must not need Element, Matrix IDs, bot commands, developer tools, manual room acceptance, appservice knowledge, or direct access to `mautrix-meta:29319`.

## Current product context

This decision is intentionally based on the current simplified architecture, not on the archived multi-tenant control-plane design.

The current deployment model is:

```text
one customer
  -> one deployed stack
     -> Synapse
     -> mautrix-meta
     -> integration/NiceGUI
     -> Chatwoot
```

A second customer gets a separate stack. Tenant multiplexing inside one mautrix process is not required for this onboarding implementation.

The current `main` stack already provides the correct product surface and trust boundary to extend:

- `integration` exposes the authenticated NiceGUI `/admin` UI;
- `integration` already persists product configuration;
- `integration` already talks to Synapse and Chatwoot;
- `integration` already exposes an authenticated internal proxy resolver to mautrix-meta;
- `mautrix-meta` remains private on the Compose network;
- the stack currently pins `dock.mau.dev/mautrix/meta:v26.08.1`.

The onboarding implementation should therefore extend `integration`; it should not introduce Element automation or a separate customer-facing Matrix client.

## Verified upstream basis

mautrix-meta uses the BridgeV2 provisioning API for integration-manager-style flows. Its provisioning configuration supports a generated/shared secret and optional Matrix-token authentication. The BridgeV2 provisioning model is stateful rather than a single OAuth redirect.

Login flows may require different step classes, including interactive input, cookie/webview collection, client HTTP work, WebAuthn, wait/display steps, and completion. The exact schema and endpoints used by our implementation must be verified against the mautrix-go version bundled in the pinned mautrix-meta release; code must not assume that the moving upstream `main` schema is identical to the pinned runtime.

The official Meta bridge authentication documentation still describes Facebook cookie login and recommends `mautrix-manager` specifically to automate extraction instead of requiring users to use browser developer tools.

## Critical browser limitation

A normal web page cannot generically replace mautrix-manager for Meta cookie extraction.

Same-origin rules, HttpOnly cookies, CSP, third-party-cookie restrictions, browser isolation, and WebAuthn origin constraints prevent our NiceGUI page from simply embedding Facebook in an iframe or opening a popup and then reading Facebook session material.

Therefore the following are explicitly rejected as the primary architecture:

- embedding facebook.com or messenger.com in an iframe and expecting to read its cookies;
- opening a normal popup and expecting the parent page to extract Meta cookies;
- asking users to copy `datr`, `c_user`, `sb`, `xs`, cURL requests, or developer-tools output;
- scripting Element to send bridge bot commands;
- exposing the mautrix appservice/provisioning port publicly so browser JavaScript can call it directly.

If the selected mautrix login flow requires privileged cookie/webview access, a capable trusted client is required. The upstream-proven model is an Electron application such as mautrix-manager. A browser extension could also satisfy some privileged operations, but adds permissions, distribution, review, and support cost.

## Target architecture

```text
Customer browser
      |
      | HTTPS
      v
NiceGUI /admin
(integration service)
      |
      | server-side authenticated calls
      | private Compose network
      v
mautrix-meta provisioning API :29319
      |
      v
Meta / Messenger
```

When a login flow cannot be completed by ordinary browser UI alone:

```text
/admin
  |
  | controlled handoff
  v
trusted local auth helper
(Electron or equivalent)
  |
  | only the required transient login material/state
  v
integration backend / private mautrix provisioning API
```

The helper is conditional, not a new permanent product layer. If a future upstream login flow is browser-safe, `/admin` should handle it directly.

## Security boundary

The browser must never receive the mautrix provisioning shared secret.

Preferred trust boundary:

```text
browser -> authenticated integration backend -> private mautrix-meta API
```

The `integration` backend is responsible for:

- authenticating the admin user;
- starting and tracking provisioning login processes;
- relaying only safe step metadata to the UI/helper;
- submitting login-step responses server-side;
- treating login-process IDs as security-sensitive transient state;
- never logging raw Meta cookies, passwords, tokens, WebAuthn assertions, provisioning secrets, or Matrix access tokens;
- normalizing bridge errors into product-facing states;
- implementing reconnect, cancel, and disconnect operations;
- confirming actual connected state before reporting success.

`mautrix-meta:29319` remains private. No public ingress is added merely for onboarding.

## Product state model

The `/admin` integration card should present product concepts only:

```text
Facebook Messenger

Status: Connected | Action required | Connecting | Disconnected | Error
Account: <display name when available>
Messenger: Active / Inactive
Marketplace: Active / Inactive
Last healthy sync: <timestamp when available>

[Connect] [Reconnect] [Disconnect]
```

A submitted login request is not success. The UI may display `Connected` only after the bridge has completed the login and the resulting remote login is usable.

Interactive checkpoints, 2FA, passkeys/WebAuthn, challenge pages, and provider errors should be represented as ordinary user actions without exposing Matrix terminology.

## Matrix room handling is part of the same UX contract

Removing Element from login is insufficient if users still have to open Element later to accept portal invitations.

The product contract is broader:

- newly bridged conversations must become usable without manual Element interaction;
- Matrix membership/invitation prerequisites must be handled automatically by the stack;
- downstream Chatwoot synchronization must not depend on a human accepting a Matrix invite;
- room display names are not authoritative routing identifiers.

Existing automatic-join and reconciliation code should remain the implementation path for this requirement rather than adding UI instructions for Element.

## Relationship to the archived multi-tenant control-plane work

The repository contains substantial multi-tenant/control-plane documentation and code history. That work solved harder identity and egress problems, including pre-created connection binding and account-aware routing.

It is not the active product deployment model on this line.

For the current one-stack-per-customer product:

- do not add tenant-selection UX to onboarding;
- do not require provisioning claims solely to solve cross-tenant ambiguity that cannot occur inside a dedicated customer stack;
- do not revive the archived control plane as a prerequisite for customer-friendly login;
- preserve useful security lessons from that work, especially fail-closed behavior, secret redaction, deterministic identity binding, and private service-to-service calls.

If the product later returns to a shared multi-tenant bridge, this ADR must be revisited rather than silently stretching the single-client model.

## Implemented architecture

The server-side provisioning adapter, managed NiceGUI onboarding state machine,
replay-resistant helper handoff, login-process recovery, secret-safe structured
diagnostics, and production navigation are implemented in `integration`.

The pinned runtime currently exposes `messenger-lite-android` as a browser-safe
`user_input` flow. That flow is the supported happy path. Repository CI covers
its BridgeV2 HTTP contract, and real-provider validation has confirmed that the
flow can complete against Meta. CI still cannot prove future Meta checkpoints or
provider-side behavior, so live acceptance remains mandatory after deployment.

### Provisioning adapter inside `integration`

Add a small server-side adapter for the pinned bridge's provisioning API.

Minimum capabilities:

- determine the authenticated bridge/Matrix identity used for provisioning;
- list supported login flows;
- start a login process;
- inspect the next required step;
- submit each supported step type needed by Facebook/Messenger;
- cancel an in-progress login;
- inspect existing logins/connection state;
- logout/disconnect;
- enforce bounded timeouts and normalized errors;
- redact secrets from logs and admin diagnostics.

The adapter must be integration-tested against the exact pinned mautrix-meta image rather than only mocked from an upstream schema.

### NiceGUI onboarding UI

Extend `/admin` with the Facebook Messenger integration card and login state machine.

The UI should:

- use clear non-Matrix language;
- survive page refresh without falsely restarting completed operations;
- show action-required states distinctly from hard failures;
- allow explicit cancel/retry;
- never render raw session cookies or bridge secrets;
- show a useful final account identity when the bridge exposes one.

### Privileged auth helper / cookie fallback

Test the actual v26.08.1 Facebook/Messenger login flows first.

When an upstream web flow returns a cookie/webview step that cannot be satisfied safely inside the normal browser, the existing trusted helper/cookie fallback may be used. It must remain secondary to Messenger Android and must never become an excuse to expose provisioning secrets or log raw Meta session material.

Helper requirements include:

- isolated temporary webview/session;
- strict Meta-origin allowlist;
- no persistent plaintext Meta credentials;
- cleanup on completion/cancel;
- authenticated, replay-resistant handoff to the current stack;
- no analytics or logs containing authentication material;
- signed/reproducible builds where practical.

### Production acceptance

Before calling managed onboarding complete, prove on the exact deployed revision:

```text
Connect from /admin
-> complete real Meta authentication
-> existing Messenger/Marketplace chats synchronize as expected
-> new inbound conversation reaches Matrix/Chatwoot without Element
-> reply reaches Meta
-> restart/redeploy preserves the session
-> reconnect works from /admin
-> disconnect works from /admin
-> no raw credentials appear in logs
```

Real-provider staging is mandatory because repository CI cannot prove Meta checkpoints, cookie behavior, provider-side blocks, or a complete real-world round trip.

## Acceptance criteria

The feature is complete only when a non-technical operator can:

1. Open `/admin`.
2. Click `Connect Facebook`.
3. Complete every required Meta authentication/checkpoint step through the product-supported flow.
4. Return automatically to `/admin` if a helper was used.
5. See an actual connected account.
6. Receive Messenger and Marketplace conversations without opening Element.
7. Reply through the normal Chatwoot/product path.
8. Restart/redeploy without repeating login in the normal case.
9. Reconnect or disconnect from `/admin`.

At no point in that happy path should the operator need Matrix terminology, bot commands, cookies, cURL, browser developer tools, or the mautrix internal port.

## Non-negotiable invariants

- Matrix/Synapse/mautrix remain infrastructure, not customer UI.
- The provisioning secret never reaches browser JavaScript.
- Raw Meta authentication material never enters normal logs.
- `mautrix-meta:29319` remains private.
- All provisioning operations are initiated by an authenticated admin session.
- Login state is treated as sensitive and bounded in lifetime.
- A failed privileged step fails visibly; it does not fall back to insecure manual cookie instructions.
- No implementation claims success until a usable bridge login exists.
- Current single-client isolation is enforced by deployment topology; multi-tenant assumptions must not be silently reintroduced.

## Consequences

Benefits:

- the customer never needs Element;
- the existing `/admin` becomes the single configuration surface;
- the design uses mautrix's supported management surface rather than UI automation;
- the bridge remains private and infrastructure complexity stays hidden;
- the one-stack-per-customer architecture keeps onboarding substantially simpler than the archived multi-tenant design.

Costs and limitations:

- current Meta authentication may still require a local helper;
- unofficial Meta protocol changes can break bridge connectivity independently of our UI;
- provisioning compatibility must be revalidated on bridge upgrades;
- a later return to shared multi-tenancy requires a new identity/authorization review.

## Final product rule

**The integration/admin panel is the product surface. Matrix, Synapse and mautrix-meta are internal transport infrastructure.**

We hide infrastructure complexity from customers, but we do not attempt to bypass browser security boundaries. When an upstream login step requires privileged browser access, we use an appropriately trusted client rather than pretending an iframe or popup can do something browsers intentionally prohibit.
