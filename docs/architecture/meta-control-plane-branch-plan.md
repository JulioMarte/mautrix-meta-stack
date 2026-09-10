# Meta Control Plane Branch Plan

Status: **Normative branch contract**  
Branch: `feature/meta-control-plane`  
Target stack: Synapse + mautrix-meta + Meta Control Plane + Chatwoot  
Initial implementation language: Bun + TypeScript + Elysia  
Initial persistence: `bun:sqlite` behind repository interfaces  

## 1. Purpose of this branch

This branch exists to turn the current single-user proof of concept into the smallest credible multi-tenant Meta messaging platform that can later be operated as a service.

The branch is not considered successful merely because it can store a proxy URL or forward a message. It is successful only if one `mautrix-meta` process can host more than one Meta login while preserving tenant identity, egress isolation, Chatwoot routing, message idempotency, and operational auditability.

The core product hypothesis to prove is:

> Two independent Meta accounts can run through one mautrix-meta instance, each account is permanently bound to its own approved residential egress, each conversation is routed to the correct Chatwoot inbox, replies travel back to the correct Meta thread, and no normal failure silently falls back to the Contabo datacenter IP.

Everything in this branch should be judged against that statement.

## 2. Research findings that constrain the design

The design must reflect the actual behavior of mautrix-meta v26.07 rather than assumptions made by the control plane.

### 2.1 `get_proxy_from` is global and identity-blind

In upstream mautrix-meta v26.07, `MetaConnector.getProxy(reason)` receives only a `reason` string. The configured `get_proxy_from` URL is called with HTTP GET and the bridge adds only:

```text
?reason=<reason>
```

The request does not contain the Matrix user, the `UserLogin.ID`, the Facebook account ID, or any tenant identifier. The response contract is only:

```json
{ "proxy_url": "socks5://..." }
```

Therefore a stock single-instance bridge cannot correctly implement per-account egress assignment through `get_proxy_from` alone.

### 2.2 The account identity is already known inside the bridge

Facebook cookie login already has access to `c_user` before the first Meta request. In the current connector this value becomes the Meta account / BridgeV2 user login identity after successful login.

This means the bridge can resolve an account-specific proxy before the first request to Facebook if we make a small, explicit patch at the boundary where the proxy resolver is called.

### 2.3 Proxy resolution happens more than once

Proxy refresh can occur for reasons such as:

```text
login
connect
reconnect
http request error: ...
```

The control plane must therefore return a stable assignment. A call to the resolver must not imply rotation.

### 2.4 Not all Meta traffic currently follows the dynamic resolver

The main HTTP/MQTT client can use `get_proxy_from`, but current media and E2EE paths rely on static proxy configuration in places. A multi-tenant system that claims egress isolation cannot treat this as acceptable leakage.

The branch must either route every relevant Meta traffic class through the same account-specific egress assignment or explicitly prove that a traffic class cannot leave the host directly.

### 2.5 One mautrix process can host multiple logins

BridgeV2 maintains independent `UserLogin` objects and each login has its own `MetaClient`. The architectural problem is not multi-login capability; the problem is that the upstream proxy resolver is connector-global.

The target architecture therefore remains one bridge process for many accounts, with a minimal maintainable patch rather than one mautrix process per tenant.

## 3. Branch-level architectural decision

The target topology is:

```text
                           Admin / API clients
                                  |
                                  v
                     Meta Connector Control Plane
                    Bun + TypeScript + Elysia
                                  |
              +-------------------+--------------------+
              |                   |                    |
              v                   v                    v
        Tenant registry      Egress registry     Chatwoot bindings
              |                   |                    |
              +-------------------+--------------------+
                                  |
                  authenticated internal contracts
                                  |
                     patched mautrix-meta v26.07
                                  |
                    +-------------+-------------+
                    |             |             |
                 login A       login B       login C
                    |             |             |
                 proxy A       proxy B       proxy C
                    |             |             |
                    +-------------+-------------+
                                  |
                                 Meta

Matrix/Synapse events --------------------------> Control Plane
Control Plane <-------------------------- Chatwoot webhooks
```

The control plane owns tenant configuration, egress assignment, Chatwoot routing, event normalization, idempotency, health state, and audit history.

mautrix-meta owns Meta protocol/session handling.

Synapse owns Matrix rooms/events.

Chatwoot owns the human support inbox/workspace.

The control plane must not become a second Meta session store.

## 4. Non-goals

This branch must not expand into unrelated product work.

Explicit non-goals:

- no generalized CRM;
- no billing system;
- no Request Engine integration;
- no orchestration of medical/business workflows;
- no storage of Meta session cookies in the control-plane database;
- no public registration flow for Synapse;
- no large frontend SPA;
- no automatic proxy rotation on every reconnect;
- no browser automation for Meta unless the bridge itself requires a user-assisted login step;
- no attempt to support every Chatwoot feature before bidirectional messaging is proven;
- no per-tenant mautrix containers unless the single-process approach is proven impossible.

## 5. Domain model

The current flat `clientes_config` proposal is rejected as the final model because it conflates tenant identity, Matrix identity, Meta identity, proxy credentials, and Chatwoot routing.

The minimum domain model is:

### 5.1 `tenants`

```text
id
slug
name
status
created_at
updated_at
```

A tenant is a customer/account boundary. It is not a Matrix user.

### 5.2 `meta_connections`

```text
id
 tenant_id
 provider                     # initially "facebook"
 meta_account_id              # Facebook account identifier once known
 mautrix_login_id             # BridgeV2 UserLogin.ID once known
 matrix_owner_mxid            # Matrix user allowed to manage this login
 chatwoot_binding_id           # nullable until configured
 egress_profile_id            # required before production activation
 egress_policy                # direct_allowed | proxy_preferred | proxy_required
 status                       # draft | ready | active | degraded | blocked | disabled
 created_at
 updated_at
```

`meta_account_id`, `mautrix_login_id`, and `matrix_owner_mxid` remain separate concepts even if two values happen to match in the first implementation.

### 5.3 `egress_profiles`

```text
id
provider
scheme
host
port
username
secret_ref
country
region
sticky_session_id
expected_exit_ip
last_verified_exit_ip
status
last_checked_at
failure_count
created_at
updated_at
```

The complete credential-bearing proxy URI should not be persisted in plaintext as the canonical representation.

The secret must be obtained through a secret reference or encrypted-at-rest mechanism whose key does not live in the same SQLite database.

### 5.4 `chatwoot_bindings`

```text
id
tenant_id
chatwoot_account_id
chatwoot_inbox_id
api_base_url
credential_ref
status
created_at
updated_at
```

### 5.5 `conversation_bindings`

```text
id
tenant_id
meta_connection_id
matrix_room_id
remote_thread_id
remote_contact_id
chatwoot_account_id
chatwoot_inbox_id
chatwoot_contact_id
chatwoot_source_id
chatwoot_conversation_id
created_at
updated_at
```

This table is the core routing bridge between Matrix/Meta and Chatwoot.

### 5.6 `processed_events`

```text
id
source
source_event_id
meta_connection_id
payload_hash
status
first_seen_at
processed_at
last_error
```

There must be a unique constraint on `(source, source_event_id)`.

### 5.7 `audit_events`

```text
id
tenant_id
actor_type
actor_id
action
entity_type
entity_id
before_json
after_json
created_at
```

Egress reassignment and connection activation/deactivation must be auditable.

## 6. Persistence strategy

The first implementation may use `bun:sqlite` because this is still a constrained deployment and SQLite lowers operational overhead.

However, application logic must not depend directly on SQLite throughout the codebase.

Create repository interfaces at the domain boundary, for example:

```ts
interface TenantRepository {}
interface MetaConnectionRepository {}
interface EgressProfileRepository {}
interface ChatwootBindingRepository {}
interface ConversationBindingRepository {}
interface ProcessedEventRepository {}
interface AuditRepository {}
```

The first adapters can be SQLite implementations.

The schema should be written so that migration to PostgreSQL does not require rewriting service logic.

SQLite is therefore an implementation choice for this phase, not a permanent architectural commitment.

## 7. Egress resolver contract

The original endpoint `GET /get-proxy?user_id=...` is rejected because upstream mautrix does not provide that identity.

The patched bridge must supply an account-specific internal contract.

Preferred initial contract:

```http
GET /internal/v1/egress/resolve?meta_account_id=<id>&login_id=<id>&reason=<reason>&traffic_class=<class>
Authorization: Bearer <internal-service-token>
```

Expected response:

```json
{
  "proxy_url": "socks5://user:secret@host:port",
  "assignment_id": "..."
}
```

`assignment_id` is for logs/audit only; mautrix may initially ignore it.

The endpoint is internal-only. It must not be exposed as a public Coolify domain.

### 7.1 Traffic classes

The resolver must be able to distinguish at least:

```text
login
messaging
media
e2ee
```

All traffic classes for the same connection should resolve to the same egress profile unless an explicit policy says otherwise.

### 7.2 Sticky semantics

Repeated resolution is not rotation.

For an active connection:

```text
resolve(account A, login)       -> egress A
resolve(account A, connect)     -> egress A
resolve(account A, reconnect)   -> egress A
resolve(account A, network err) -> egress A
```

Only an explicit administrative reassignment may change the egress profile.

### 7.3 Fail-closed semantics

For `proxy_required` connections:

```text
missing assignment -> fail
unhealthy proxy    -> fail
resolver timeout   -> fail
invalid secret     -> fail
```

The system must never silently fall back to the Contabo host IP.

`direct_allowed` exists only for deliberate test/development use.

## 8. Minimal mautrix-meta patch

The fork must remain as close to upstream v26.07 as possible.

The patch should be isolated behind a small resolver abstraction rather than spread across Meta protocol code.

Conceptual target:

```go
type ProxyContext struct {
    MetaAccountID string
    LoginID       string
    Reason        string
    TrafficClass  string
}

ResolveProxy(ctx ProxyContext) (string, error)
```

### 8.1 Login path

Before the first Meta request, derive `MetaAccountID` from the submitted Facebook cookie `c_user` and resolve egress before validating the session.

The invariant is:

> A `proxy_required` connection must never perform its first Meta network request using the Contabo address.

### 8.2 Connected login path

Once BridgeV2 has a `UserLogin`, pass both the known Meta account identifier and `UserLogin.ID` to the resolver for connect/reconnect operations.

### 8.3 Media and E2EE

The patch is incomplete until media and E2EE paths use the connection-specific resolved egress or are otherwise proven unable to bypass it.

No merge should claim tenant egress isolation while those code paths can use a connector-global proxy or direct host egress.

### 8.4 Upstream maintainability

Every fork change must:

- be isolated to as few files as practical;
- avoid unrelated formatting/refactors;
- include tests around proxy context propagation;
- be documented in `docs/architecture/mautrix-meta-fork-delta.md` before release;
- make rebasing onto newer upstream tags mechanically understandable.

If the patch grows into a broad fork, stop and reassess the architecture.

## 9. Chatwoot adapter contract

The control plane, not mautrix-meta, owns the Chatwoot integration.

### 9.1 Inbound direction

```text
Meta
  -> mautrix-meta
  -> Matrix event
  -> Matrix adapter
  -> normalized internal message
  -> conversation binding lookup/create
  -> Chatwoot API message
```

### 9.2 Outbound direction

```text
Chatwoot agent reply
  -> Chatwoot webhook
  -> verify/authenticate webhook
  -> anti-loop/idempotency check
  -> conversation binding lookup
  -> Matrix Client API send
  -> mautrix-meta
  -> Meta thread
```

### 9.3 Normalized message contract

At minimum:

```ts
type NormalizedMessage = {
  tenantId: string
  connectionId: string
  conversationExternalId: string
  messageExternalId: string
  senderExternalId: string
  senderDisplayName?: string
  direction: "inbound" | "outbound"
  text?: string
  attachments: Attachment[]
  occurredAt: string
}
```

Do not allow Chatwoot-specific payload details to become the internal domain model.

### 9.4 Idempotency and echo protection

The implementation must survive:

- Matrix redelivery;
- Chatwoot webhook retry;
- network timeout after a successful remote POST;
- process restart;
- duplicate remote events;
- the adapter seeing its own reflected message.

Every external message/event identifier must be recorded before performing side effects when practical.

## 10. Administrative surface

The first admin UI is intentionally small and server-rendered.

It should allow an operator to:

- create/disable a tenant;
- create a Meta connection;
- bind Matrix owner identity;
- assign/reassign an egress profile;
- bind a Chatwoot inbox;
- inspect connection and proxy health;
- inspect the last routing/audit events;
- disable a connection immediately.

It must not display raw proxy passwords.

The admin surface must be protected. Cloudflare Access is acceptable for the initial deployment, but internal state-changing endpoints still require CSRF-safe semantics and authenticated requests.

## 11. Public and internal API split

The service should expose separate surfaces conceptually:

```text
/admin/*              operator UI
/api/v1/*             authenticated management API
/internal/v1/*        service-to-service only
/webhooks/chatwoot    Chatwoot ingress
/health/live          liveness
/health/ready         readiness
```

The egress resolver belongs under `/internal/v1`, not `/api/v1`.

## 12. Secret handling

Secrets include:

- proxy passwords;
- Chatwoot API tokens;
- control-plane internal service token;
- future webhook signing secrets.

Rules:

1. Never commit a real secret to Git.
2. Never log a complete proxy URI containing credentials.
3. Never render secrets back into the admin HTML.
4. Never use query parameters for service authentication.
5. Secret references may be persisted; raw credentials should come from environment/Coolify secrets or an encryption layer.
6. Meta cookies remain owned by mautrix-meta and must not be copied into the control-plane database.

## 13. Observability requirements

At minimum expose structured logs containing:

```text
tenant_id
connection_id
meta_account_id when known
matrix_room_id when relevant
chatwoot_conversation_id when relevant
egress_assignment_id
traffic_class
operation
result
latency_ms
```

Never log session cookies or proxy passwords.

Metrics should eventually include:

```text
active_connections
egress_resolution_total
egress_resolution_failures
proxy_health_failures
matrix_events_received
chatwoot_messages_sent
chatwoot_webhooks_received
duplicate_events_dropped
routing_failures
```

OpenTelemetry can be added later, but the service boundaries should make it straightforward.

## 14. Required test strategy

The branch must not rely on manual success in Element as its primary proof.

### 14.1 Unit tests

Must cover:

- tenant isolation;
- egress lookup;
- fail-closed policy;
- stable sticky assignment;
- secret redaction;
- event idempotency;
- conversation binding lookup;
- Chatwoot echo suppression.

### 14.2 Contract tests

Must prove:

- mautrix resolver request contains the expected account/login context;
- control-plane resolver response is accepted by the patched bridge;
- invalid/missing assignment prevents connection when `proxy_required`;
- Chatwoot webhook payload maps to the normalized event contract;
- Matrix event maps to the normalized event contract.

### 14.3 Integration tests

Use test doubles for Meta and Chatwoot where needed, but run real Synapse/mautrix/control-plane containers in CI for the wiring itself.

### 14.4 Egress isolation proof

Before this branch can claim production-ready multi-tenancy, demonstrate with two distinct accounts/connections:

```text
connection A -> residential exit IP A
connection B -> residential exit IP B
```

and prove that login, reconnect, media, and E2EE-relevant paths cannot silently use the Contabo IP under `proxy_required`.

This proof is the most important security/operational test in the branch.

## 15. Implementation phases

### Phase 0 — Documentation and contracts

Deliver:

- this branch plan;
- fork delta plan;
- schema/migration plan;
- internal API contracts;
- event contracts;
- threat/failure model.

No broad implementation before these contracts are coherent.

### Phase 1 — Control-plane skeleton

Deliver:

- Bun/Elysia service;
- health endpoints;
- SQLite initialization/migrations;
- repository interfaces;
- tenant/meta connection/egress/chatwoot data model;
- authenticated minimal admin surface;
- no live Meta routing yet.

### Phase 2 — Egress resolver

Deliver:

- internal resolver API;
- sticky assignment;
- fail-closed policy;
- proxy health model;
- secret redaction;
- audit events;
- resolver contract tests.

### Phase 3 — Minimal mautrix fork

Deliver:

- account-aware resolver context;
- first-login egress assignment before Meta request;
- connect/reconnect propagation;
- media/E2EE treatment;
- fork delta documentation;
- CI against pinned upstream version.

### Phase 4 — Matrix to Chatwoot

Deliver:

- Matrix event ingestion;
- normalized message model;
- Chatwoot contact/conversation/message creation;
- conversation bindings;
- idempotency.

### Phase 5 — Chatwoot to Matrix

Deliver:

- webhook receiver;
- authentication/verification appropriate to the supported Chatwoot mechanism;
- outbound Matrix send;
- echo suppression;
- retry-safe behavior.

### Phase 6 — Multi-tenant proof

Deliver:

- two tenants/connections;
- distinct egress assignments;
- distinct Chatwoot inbox routing;
- simultaneous bidirectional conversations;
- fault injection for proxy failure, Chatwoot failure, duplicate webhook, and service restart;
- evidence that no `proxy_required` account falls back to direct Contabo egress.

## 16. Definition of Done

This branch is done only when all of the following are true:

- one deployed mautrix-meta process hosts at least two Meta logins;
- each login is associated with the correct tenant and Meta connection record;
- each login resolves to its own stable egress profile;
- `proxy_required` fails closed;
- login/connect/reconnect/media/E2EE-relevant network paths satisfy the egress isolation policy;
- inbound Meta messages reach the correct Chatwoot inbox/conversation;
- Chatwoot replies reach the correct Matrix room and Meta thread;
- duplicate delivery and echo loops are prevented;
- restart/redeploy does not lose routing identity;
- secrets are not committed, logged, or rendered;
- admin actions affecting routing/egress are auditable;
- CI exercises the real container topology and critical contracts;
- the mautrix fork delta is small, documented, and rebaseable.

A green CI that only proves containers start is not sufficient.

## 17. Stop conditions

Stop implementation and reassess if any of these become true:

- the mautrix fork requires broad invasive changes across protocol internals;
- account-specific egress cannot be made consistent across all relevant Meta traffic classes;
- Chatwoot routing requires storing Meta session credentials in the control plane;
- SQLite concurrency becomes a material operational bottleneck before the MVP is proven;
- single-process mautrix introduces cross-tenant state leakage that cannot be cleanly isolated;
- a required behavior depends on undocumented Meta behavior that cannot be tested deterministically.

In those cases, the correct response is architectural reassessment, not layering workarounds on top.

## 18. First implementation checkpoint

The first coding checkpoint after this document should not be the full bridge integration.

It should prove only:

```text
Control Plane starts
-> migrations run
-> tenant can be created
-> Meta connection can be created
-> egress profile can be assigned
-> resolver returns that same assignment repeatedly
-> missing assignment with proxy_required fails closed
-> no secret appears in logs or admin output
```

Once that is green and the contracts remain coherent, proceed to the mautrix patch.

## 19. Branch operating rule

This branch is intentionally isolated from `main` while architecture and implementation evolve. Coolify deploys from `main`, so incomplete work in this branch must not trigger production deployment.

Changes should reach `main` only after the branch reaches a deliberate integration checkpoint with passing CI and a reviewed deployment plan.
