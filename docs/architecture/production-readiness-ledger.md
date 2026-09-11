# Production Readiness Ledger

Status: authoritative current-state ledger for the Meta Control Plane workstream.

This document exists to prevent green phase CI from being mistaken for end-to-end production readiness. It records what is actually proven, what remains unproven, and which gaps are automatic versus human/staging-only.

## Promotion rule

`main` is the current human-accepted deployment baseline. No automated agent, bot, CI workflow or unattended release process may promote `dev` to `main`.

A human owner must explicitly approve the exact candidate SHA after reviewing the evidence and comparing it with the currently working `main` deployment. Until that instruction exists, `main` is unchanged.

## Evidence already established in repository CI

The integrated `dev` line has deterministic evidence for the following implementation slices:

- Control-plane startup, migrations, authenticated management boundary and persistent SQLite state.
- Sticky per-connection egress resolution with fail-closed behavior and secret redaction.
- Reproducible mautrix-meta source patching against pinned upstream `ed37c9e6ce47e83dc75b9abea7b636302715b9bc`.
- Account-aware login/messaging/media/E2EE proxy-context tests and controlled direct-egress sentinels.
- Matrix-event-to-Chatwoot domain/HTTP path through the internal Matrix ingress boundary, including representative attachments and retry/idempotency behavior.
- Signed Chatwoot-webhook-to-Matrix send path, routing, attachments, provenance, duplicate suppression and ambiguous-response reconciliation.
- Two-tenant controlled A/B routing/egress topology, restart persistence, assigned-proxy failure and secret-canary checks.
- Cold backup and restore of the complete control-plane data volume on a disposable environment, including tenant, Meta connection, sticky egress assignment, Chatwoot/conversation binding, processed-event and audit recovery.

These facts are strong engineering evidence. They are not equivalent to the global Definition of Done.

## Blocking gaps discovered by contract audit

### 1. Pre-Meta provisioning claim is not implemented

`meta-control-plane-onboarding-identity-binding.md` requires a short-lived, single-purpose provisioning claim or equivalent opaque bootstrap identifier that binds an authorized Matrix principal to exactly one pre-created `meta_connection` before any Meta-bound request.

The current schema has no `provisioning_claims` table and the current implementation does not provide the required issue/consume/revoke lifecycle. Resolving only by `meta_account_id`/`mautrix_login_id` does not prove the bootstrap contract for one Matrix owner managing multiple Meta connections.

**Status:** release-blocking technical gap.

**Required completion evidence:** migration + repository/service/API contract + expiry/use/revocation/conflict tests + fork/login propagation proof showing the claim and `c_user` bind the intended connection before first Meta network access.

### 2. Real Matrix ingestion mechanism is not implemented/proven

`meta-control-plane-matrix-adapter.md` requires one concrete ingestion mechanism, a dedicated Matrix service identity, durable checkpoint/recovery semantics and CI against a disposable Synapse. The current Matrix -> Chatwoot implementation begins at `POST /internal/v1/matrix/events`; that proves the normalized internal boundary, not ingestion from Synapse.

**Status:** release-blocking technical gap.

**Required completion evidence:** choose `/sync`/sliding-sync or an Application Service push design; implement the adapter; prove service authentication, room attribution, checkpoint/restart, unbound/ambiguous rejection, provenance loop suppression, two-tenant room separation and representative attachment flow against real disposable Synapse.

### 3. Room attribution signal is not yet proven against the pinned bridge

The Matrix adapter contract forbids routing by room name/display name and requires stable verifiable bridge/Matrix metadata to map `matrix_room_id -> meta_connection_id`.

The current internal ingress receives `connectionId` and remote identifiers from a trusted internal caller; this does not by itself establish how a raw room discovered from Synapse is attributed to the correct connection.

**Status:** release-blocking technical gap coupled to Matrix ingestion.

### 4. Full Chatwoot tenancy contract is broader than the current Phase 4/5 gates

The normative Chatwoot tenancy contract also calls for proof around colliding numeric IDs across separate installation/account contexts, tenant-isolated remote-contact identity, and explicit semantics for changing an active inbox without silently rewriting historical conversation bindings.

Current Phase 6 proves separate account/inbox routing in one controlled topology, but that is not identical to every tenancy assertion above.

**Status:** contract-coverage gap requiring targeted CI review/tests. It may be closed by existing implementation plus stronger tests, or may reveal missing behavior.

### 5. Real Meta/provider deployment evidence does not exist yet

Repository CI intentionally does not use real Facebook customer credentials or paid residential proxies. Therefore it has not proven:

- one deployed mautrix process hosting at least two real Meta logins;
- actual Meta acceptance for both sessions;
- actual provider exit IP/country for each account;
- public-network absence of host/Contabo egress in the deployed environment;
- real Meta -> Matrix -> Chatwoot -> Matrix -> Meta round trip;
- behavior through real Coolify/DNS/TLS/network policy.

**Status:** mandatory controlled staging/human evidence. This cannot be honestly replaced by test doubles.

### 6. Production backup operations are not proven by CI alone

The repository now proves the recovery algorithm on disposable Docker volumes. It does not prove the operator's real backup storage, encryption/access control, retention schedule, off-host durability, restore permissions or Coolify-specific volume identifiers.

**Status:** production-operations/human evidence gap.

## Non-blocking but material debt

The following are known limitations that should remain visible even if they do not currently block the Facebook/Messenger MVP acceptance path:

- Dynamic Instagram identity is explicitly unsupported until its identity transition is modeled and tested.
- Request-scoped media transports may create avoidable transient connection/resource overhead under heavy chunked media usage.
- Source reconstruction is deterministic but container/action/package inputs are not yet fully hermetic or digest-pinned.
- Production operator RBAC, retention policy, metrics/OpenTelemetry and PostgreSQL migration remain deferred according to the architecture docs.

## Completion sequence from current state

The remaining engineering sequence is:

1. integrate the backup/restore + promotion-policy documentation into `dev` after exact-SHA CI;
2. implement the provisioning-claim bootstrap contract on a focused branch from green `dev`;
3. implement the concrete Matrix ingestion/room-attribution/checkpoint contract on a focused branch from green `dev`;
4. close any remaining Chatwoot tenancy assertions with deterministic tests;
5. rerun the full exact-SHA repository acceptance matrix on `dev`;
6. prepare a staging checklist with every non-CI assertion and explicit pass/fail evidence fields;
7. human operator performs controlled staging with real disposable Meta accounts and real egress endpoints;
8. human owner evaluates the resulting `dev` candidate against the currently working `main` deployment;
9. only an explicit human instruction for the exact SHA may authorize a later `main` promotion.

## Current readiness statement

The repository is **not production-ready yet** under its own normative documents.

It has substantial integrated deterministic coverage, including the multi-tenant Phase 6 topology, but at least two major implementation contracts remain incomplete: pre-Meta provisioning claims and real Matrix ingestion/room attribution. Real Meta/provider staging evidence also remains outstanding.

Any stronger statement would overstate the evidence.
