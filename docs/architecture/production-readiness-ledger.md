# Production Readiness Ledger

Status: authoritative current-state ledger for the Meta Control Plane workstream.

This document exists to prevent green phase CI from being mistaken for end-to-end production readiness. It records what is actually proven, what remains unproven, and which gaps are automatic versus human/staging-only.

## Promotion rule

`main` is the current human-accepted deployment baseline. No automated agent, bot, CI workflow or unattended release process may promote `dev` to `main`.

A human owner must explicitly approve the exact candidate SHA after reviewing the evidence and comparing it with the currently working `main` deployment. Until that instruction exists, `main` is unchanged.

## Evidence already established on integrated `dev`

The integrated `dev` line has deterministic evidence for:

- control-plane startup, migrations, authenticated management boundary and persistent SQLite state;
- sticky per-connection egress resolution with fail-closed behavior and secret redaction;
- reproducible mautrix-meta source patching against pinned upstream `ed37c9e6ce47e83dc75b9abea7b636302715b9bc`;
- account-aware login/messaging/media/E2EE proxy-context tests and controlled direct-egress sentinels;
- Matrix-event-to-Chatwoot behavior beginning at the normalized internal Matrix ingress boundary;
- signed Chatwoot-webhook-to-Matrix send path, routing, attachments, provenance, duplicate suppression and ambiguous-response reconciliation;
- two-tenant controlled A/B routing/egress topology, restart persistence, assigned-proxy failure and secret-canary checks;
- cold backup and restore of the complete control-plane data volume on a disposable environment.

The last fully integrated green baseline before the provisioning branch is `dev@a58e354cb12c808659fbabcdb338c1c499ab8cba`.

These facts are strong engineering evidence. They are not equivalent to the global Definition of Done.

## Current implementation branch: provisioning bootstrap

`feature/provisioning-claim-bootstrap` implements the previously missing pre-Meta identity bootstrap. Until this branch is exact-SHA green, merged to `dev`, and the resulting `dev` merge SHA is green, this section is **implemented on branch**, not yet **integrated**.

Implemented behavior includes:

- schema v3 `provisioning_claims` with digest-only secret persistence, expiry, use and revocation state;
- operator claim issue/revoke API;
- service-authenticated atomic claim consumption binding locally extracted Facebook `c_user` to one pre-created `meta_connection`;
- Matrix-owner, tenant, account and login-ID conflict checks;
- dynamic provisioning restricted to healthy assigned `proxy_required` egress;
- BridgeV2 `user_input` claim step before cookies for dynamic Facebook/Messenger login;
- confidential bootstrap proxy returned by claim consumption and installed before provider transport;
- observable direct-sentinel test intended to prove provider HTTP does not run before successful provisioning/proxy installation;
- service binding of `mautrix_login_id` to the already provisioned connection;
- no Meta cookies sent to or stored by the control plane.

**Current status:** implementation in acceptance. Do not mark this blocker closed until the exact branch candidate and post-merge `dev` matrix are green.

## Remaining blocking gaps

### 1. Real Matrix ingestion mechanism is not implemented/proven

`meta-control-plane-matrix-adapter.md` requires one concrete ingestion mechanism, a dedicated Matrix service identity, durable checkpoint/recovery semantics and CI against a disposable Synapse. The current Matrix -> Chatwoot implementation begins at `POST /internal/v1/matrix/events`; that proves the normalized internal boundary, not ingestion from Synapse.

**Status:** release-blocking technical gap.

**Required completion evidence:** choose `/sync`/sliding-sync or an Application Service push design; implement the adapter; prove service authentication, exact room attribution, checkpoint/restart, unbound/ambiguous rejection, provenance loop suppression, two-tenant room separation and representative attachment flow against real disposable Synapse.

### 2. Room attribution signal is not yet proven against the pinned bridge

The Matrix adapter contract forbids routing by room name/display name and requires stable verifiable bridge/Matrix metadata to map `matrix_room_id -> meta_connection_id`.

The current internal ingress receives `connectionId` and remote identifiers from a trusted internal caller; this does not establish how a raw room discovered from Synapse is attributed to the correct connection.

**Status:** release-blocking technical gap coupled to Matrix ingestion.

### 3. Full Chatwoot tenancy contract is broader than current gates

The normative Chatwoot tenancy contract also calls for proof around colliding numeric IDs across separate installation/account contexts, tenant-isolated remote-contact identity, and explicit semantics for changing an active inbox without silently rewriting historical conversation bindings.

Current Phase 6 proves separate account/inbox routing in one controlled topology, but that is not identical to every tenancy assertion above.

**Status:** contract-coverage gap requiring targeted CI review/tests. Existing code may already satisfy part of it; the missing evidence must be added rather than assumed.

### 4. Real Meta/provider deployment evidence does not exist yet

Repository CI intentionally does not use real Facebook customer credentials or paid residential proxies. Therefore it has not proven:

- one deployed mautrix process hosting at least two real Meta logins;
- actual Meta acceptance for both sessions;
- actual provider exit IP/country for each account;
- public-network absence of host/Contabo egress in the deployed environment;
- real Meta -> Matrix -> Chatwoot -> Matrix -> Meta round trip;
- behavior through real Coolify/DNS/TLS/network policy.

**Status:** mandatory controlled staging/human evidence. This cannot be replaced honestly by test doubles.

### 5. Production backup operations are not proven by CI alone

The repository proves the recovery algorithm on disposable Docker volumes. It does not prove the operator's real backup storage, encryption/access control, retention schedule, off-host durability, restore permissions or Coolify-specific volume identifiers.

**Status:** production-operations/human evidence gap.

## Non-blocking but material debt

Known limitations that remain visible even if they do not block the Facebook/Messenger MVP acceptance path:

- dynamic Instagram identity is explicitly unsupported until its identity transition is modeled and tested;
- Messenger Lite dynamic egress remains unsupported because stable pre-network identity is unavailable in that flow;
- request-scoped media transports may create avoidable transient connection/resource overhead under heavy chunked media usage;
- source reconstruction is deterministic but container/action/package inputs are not fully hermetic or digest-pinned;
- the fork delta is currently composed from multiple deterministic Python patch/fixup scripts and should be consolidated after correctness is stable;
- production operator RBAC, retention policy, metrics/OpenTelemetry and PostgreSQL migration remain deferred according to architecture docs.

## Completion sequence from current state

1. finish exact-SHA provisioning acceptance, update documentation, merge only to `dev`, then verify the resulting `dev` SHA;
2. implement concrete Matrix ingestion + exact room attribution + durable checkpointing on a new focused branch from green `dev`;
3. close remaining Chatwoot tenancy assertions with targeted deterministic tests and any code fixes they expose;
4. rerun the complete repository acceptance matrix on the resulting `dev` candidate;
5. prepare a staging checklist containing every assertion CI cannot make and explicit evidence fields;
6. human operator performs controlled staging with real disposable Meta accounts and real egress endpoints;
7. perform the real backup/restore operational drill and capture evidence;
8. human owner evaluates the exact `dev` candidate against the currently working `main` deployment;
9. only an explicit human instruction naming/approving the exact SHA may authorize a later `main` promotion.

## Current readiness statement

The repository is **not production-ready yet** under its own normative documents.

Provisioning bootstrap is now implemented on a feature branch and undergoing acceptance, but real Matrix ingestion/room attribution remains a major unimplemented contract. Chatwoot tenancy coverage, real Meta/provider staging and real production-operations evidence also remain outstanding.

No CI result or branch merge changes the human-controlled `main` promotion rule.