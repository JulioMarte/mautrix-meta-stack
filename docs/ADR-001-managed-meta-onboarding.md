# ADR-001: Managed Meta onboarding without Element

Status: Accepted direction, implementation pending

Date: 2026-09-15

## Decision

The product-facing onboarding flow for Facebook Messenger / Marketplace will be owned by our integration panel. Matrix, Synapse, bridge bot rooms, and Element are infrastructure details and must not be part of the normal customer workflow.

The integration panel will orchestrate mautrix-meta through its current bridgev2 provisioning API. It must not automate Element UI actions or rely on users manually sending bot commands.

However, the browser-only panel must not pretend that it can perform every Meta authentication step itself. The current Meta login flow may require cookie extraction, request/header capture, WebAuthn, or other client-side steps that ordinary web pages cannot perform across facebook.com / messenger.com origins. For those flows, the product will need a trusted local helper (for example an Electron-based helper derived from the same model as mautrix-manager) or another upstream-supported mechanism. An iframe or normal popup is not an acceptable architecture for cookie extraction.

This ADR is the source of truth for the onboarding direction until superseded by a newer ADR.

## Why this is the direction

A non-technical user should experience:

```text
Integrations
  -> Facebook Messenger
  -> Connect account
  -> complete the authentication steps requested by the bridge
  -> Connected
  -> Messenger / Marketplace sync starts
```

The user should not need to know about:

- Matrix IDs
- Element
- bridge management rooms
- bot commands
- appservice registrations
- mautrix internal ports
- copying cookies from browser developer tools
- manually accepting Matrix room invitations

The current repository is a transport PoC. The product UX should sit above that transport rather than expose it.

## Verified upstream capabilities

The current stack pins `dock.mau.dev/mautrix/meta:v26.07`.

For v26.07, mautrix-meta exposes the bridgev2 provisioning API and its generated configuration includes:

```yaml
provisioning:
  shared_secret: generate
  allow_matrix_auth: true
  debug_endpoints: false
  enable_session_transfers: false
  fail_on_webauthn: false
```

Relevant upstream sources:

- mautrix-meta v26.07 configuration: https://docs.mau.fi/configs/mautrix-meta/v26.07.html
- bridgev2 provisioning OpenAPI: https://github.com/mautrix/go/blob/main/bridgev2/matrix/provisioning.yaml
- Meta authentication documentation: https://docs.mau.fi/bridges/go/meta/authentication.html
- mautrix-manager: https://github.com/mautrix/manager

The provisioning API is not limited to a single username/password form. The current API models login as a state machine and can return steps including:

- `user_input`
- `cookies`
- `client_http`
- `webauthn`
- `display_and_wait`
- `complete`

The important endpoints include:

```text
GET  /_matrix/provision/v3/whoami
GET  /_matrix/provision/v3/login/flows
POST /_matrix/provision/v3/login/start/{flowID}
POST /_matrix/provision/v3/login/step/{loginProcessID}/{stepID}/user_input
POST /_matrix/provision/v3/login/step/{loginProcessID}/{stepID}/cookies
POST /_matrix/provision/v3/login/step/{loginProcessID}/{stepID}/client_http
POST /_matrix/provision/v3/login/step/{loginProcessID}/{stepID}/webauthn
POST /_matrix/provision/v3/login/step/{loginProcessID}/{stepID}/display_and_wait
POST /_matrix/provision/v3/login/cancel/{loginProcessID}
POST /_matrix/provision/v3/logout/{loginID}
```

Endpoint details must be verified against the exact mautrix-go version bundled by the pinned mautrix-meta release before implementation. We must not copy a moving `main` OpenAPI schema into production code and assume it is identical to every pinned bridge release.

## Important correction: a web page cannot replace mautrix-manager for cookie extraction

The original idea of putting Facebook login in an iframe or normal popup is not sufficient.

The maintainer of mautrix-manager explicitly documents why the current manager is an Electron application: a normal web application cannot extract cookies from another origin. Browser same-origin policy, HttpOnly cookies, CSP, third-party-cookie restrictions, and WebAuthn origin rules make a generic web-only cookie-capture flow unreliable or impossible.

The provisioning API reflects this reality. A `cookies` login step includes information intended for a capable webview/client, such as:

- URL to load
- optional user agent
- URL completion pattern
- fields to extract
- cookie domains
- optional extraction JavaScript

Therefore:

- Do not build an iframe solution for Facebook authentication.
- Do not promise that a normal browser popup can return Meta cookies to our backend.
- Do not ask normal customers to open developer tools and copy cookies.
- Do not automate Element as a substitute for using the provisioning API.

## Target architecture

```text
Customer browser
      |
      | HTTPS
      v
Integration panel / product backend
      |
      | authenticated server-to-server calls
      | private network only
      v
mautrix-meta :29319
      |
      v
Meta / Messenger

Optional when a login flow requires privileged browser access:

Customer browser
      |
      | launches / hands off login
      v
Trusted local auth helper
(Electron or equivalent capable client)
      |
      | provisioning login state + extracted login material
      v
Product backend / mautrix provisioning API
```

The local helper is not necessarily required for every future login method. The panel must inspect the login flow returned by mautrix and render/dispatch the step according to its type.

## Backend boundary

The browser must not receive the mautrix provisioning `shared_secret`.

The preferred boundary is:

```text
Browser -> our authenticated API -> private mautrix-meta provisioning API
```

The provisioning API should remain private. `mautrix-meta:29319` should not be exposed directly to the public Internet merely to support onboarding.

Our backend is responsible for:

- authenticating the product user
- authorizing which tenant/account may control which Matrix/mautrix identity
- mapping the product integration record to the corresponding Matrix user and mautrix login ID
- initiating login flows
- relaying safe login-step metadata to the UI/helper
- validating state and anti-CSRF/nonces for handoffs
- never logging raw cookies, passwords, access tokens, WebAuthn assertions, or bridge shared secrets
- presenting connection state in product language
- invoking logout/reconnect operations

## Identity model

For the current single-account PoC, one Matrix admin identity exists. That is acceptable only for validation.

For a real multi-tenant product, a single shared Matrix admin identity must not become the identity of every customer. Before multi-tenant rollout we must define an isolation model covering at least:

- one product tenant -> one controlled Matrix user identity (or another upstream-supported isolation boundary)
- one or more mautrix remote-account login IDs attached to that identity
- authorization rules preventing one tenant from enumerating or controlling another tenant's login IDs
- database isolation / ownership
- cleanup semantics when a tenant disconnects
- auditability without storing secrets in logs

Do not treat the current PoC admin account as the final tenant model.

## Product UX contract

The integration page should eventually expose product concepts only:

```text
Facebook Messenger
Status: Connected / Action required / Disconnected
Account: <remote account display name when available>
Messenger: Active
Marketplace: Active
Last healthy sync: <timestamp>

[Connect] [Reconnect] [Disconnect]
```

If mautrix reports a checkpoint, 2FA requirement, WebAuthn step, or login failure, the UI should describe the user action required without exposing Matrix implementation details.

The UI must not claim success merely because a login request was submitted. Success means the provisioning state reaches `complete` and the resulting login reports a usable connected state.

## Room and invitation handling

Normal customers must not have to open Element to accept rooms or bridge invitations.

The final product architecture must automatically handle the Matrix-side prerequisites required for newly bridged conversations. This includes ensuring that portal creation/invitation behavior, membership, and downstream Chatwoot synchronization do not depend on a human accepting an Element invitation.

This is a separate implementation concern from Meta authentication, but it is part of the same UX contract: Matrix is internal infrastructure.

## What we are explicitly not building

We will not build the following as the primary product path:

1. Element automation or browser scripting that clicks through the Matrix client.
2. An iframe embedding facebook.com or messenger.com as a generic authentication solution.
3. A public exposure of the mautrix appservice/provisioning port just so the browser can call it directly.
4. A workflow that asks non-technical customers to copy `datr`, `c_user`, `sb`, `xs`, cURL requests, or developer-tools output.
5. A custom reimplementation of Meta's private protocol when mautrix already owns that compatibility layer.
6. A product architecture tied to one specific login flow ID. Login flows and step types must be discovered from the provisioning API.

## Version risk and upgrade gate

As of 2026-09-15, this repository still pins mautrix-meta v26.07. Upstream has already released v26.08.1, and that release contains a fix for a breaking change on Messenger servers.

Source: https://github.com/mautrix/meta/releases

This does not mean we should blindly switch to `latest`. It means the current pin is now known to be behind a connection-compatibility fix.

Before implementing the managed onboarding API, create a dedicated upgrade task to:

1. Review v26.08 and v26.08.1 changelogs.
2. Verify configuration/schema changes from v26.07.
3. Verify the exact provisioning OpenAPI behavior shipped by the chosen version.
4. Test Messenger and Marketplace send/receive/backfill/reconnect.
5. Only then update the pinned image.

Keep using explicit release pins.

## Implementation phases

### Phase 0 - transport reliability

Keep proving the existing Messenger / Marketplace transport loop:

```text
remote login
  -> existing chats sync
  -> inbound message reaches Matrix
  -> Matrix reply reaches Facebook
  -> restart/redeploy
  -> bridge reconnects
  -> missed Marketplace messages backfill
```

This remains a prerequisite. A polished onboarding flow cannot compensate for an unreliable bridge.

### Phase 1 - provisioning API adapter

Implement a private backend adapter around the bridgev2 provisioning API.

Minimum capabilities:

- `whoami`
- list login flows
- start login
- submit each supported step type
- cancel login
- list/current login state
- logout
- normalized errors and timeouts

The adapter must be version-aware and covered by integration tests against the pinned bridge image.

### Phase 2 - integration panel

Add the Facebook Messenger integration screen and state machine.

The panel renders ordinary steps itself and delegates privileged cookie/webview steps to the local helper when required.

### Phase 3 - local authentication helper

If the selected Meta login flow still requires cookie extraction, build or package a trusted desktop helper. Prefer reusing the design/protocol model demonstrated by mautrix-manager rather than inventing a browser bypass.

Security requirements:

- signed builds where practical
- no persistent plaintext credential storage
- isolated temporary webview/session
- explicit origin allowlist
- secrets transmitted only to our backend/bridge over TLS
- no analytics containing authentication material
- clear session cleanup after completion/cancel

A browser extension is a possible alternative, but it carries its own permissions, store-distribution, and support burden. Electron is currently the upstream-proven approach.

### Phase 4 - tenant isolation and Chatwoot integration

Only after transport and onboarding are stable:

- define tenant-to-Matrix/mautrix identity isolation
- migrate production databases from the single-account SQLite PoC design to PostgreSQL as required
- automate Matrix membership/invitation prerequisites
- connect the resulting conversations to Chatwoot
- add observability, reconnect workflows, backups, and operator tooling

## Acceptance criteria for the final onboarding

The managed onboarding feature is not complete until a non-technical user can:

1. Open the product integration page.
2. Click Connect Facebook.
3. Complete Meta login, 2FA/checkpoints/passkey steps when required.
4. Return automatically to the product.
5. See a confirmed connected account.
6. Receive a new Messenger/Marketplace conversation without opening Element.
7. Reply through the product/Chatwoot path.
8. Restart/redeploy the bridge without repeating login under normal conditions.
9. Reconnect or disconnect the account from the integration page.

At no point in that happy path should the user need Matrix terminology or developer tools.

## Security invariants

These are non-negotiable:

- mautrix provisioning secret never reaches browser JavaScript
- raw Meta cookies/tokens/passwords never enter application logs
- mautrix appservice port remains private
- per-tenant authorization is checked server-side on every integration operation
- no tenant may choose an arbitrary Matrix user/login ID and thereby access another tenant
- login-process identifiers are treated as security-sensitive transient state
- reconnect/logout operations require normal product authorization
- secrets are stored only where required by Synapse/mautrix and protected as runtime state
- production backups containing bridge databases are treated as sensitive

## Consequences

Positive:

- normal customers do not need Element
- we use the supported bridge management surface instead of UI automation
- the product can adapt to multiple login step types
- Matrix stays an internal transport layer
- the architecture can grow toward multi-tenant operation

Costs / limitations:

- browser-only onboarding is not sufficient for current cookie-based Meta authentication
- a desktop helper may be necessary
- unofficial Meta protocol changes can still break the bridge independently of our UX
- provisioning API compatibility must be tested against each pinned bridge upgrade
- the current single-user SQLite PoC is not a production multi-tenant architecture

## Final principle

**Matrix/Synapse/mautrix are infrastructure. The integration panel is the product interface.**

We will hide infrastructure complexity, but we will not hide technical reality from ourselves: when Meta authentication requires privileged cookie/webview access, we will use a capable trusted client instead of pretending a normal webpage can bypass browser security controls.
