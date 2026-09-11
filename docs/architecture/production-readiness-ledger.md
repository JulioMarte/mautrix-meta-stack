# Production Readiness Ledger

Status: authoritative current-state ledger for the Meta Control Plane workstream.

This document prevents green phase CI from being mistaken for end-to-end production readiness. It records what is proven, what is implemented but not integrated, and what still requires staging or human evidence.

## Promotion rule

`main` is the current human-accepted deployment baseline. No automated agent, bot, CI workflow or unattended release process may promote `dev` to `main`.

A human owner must explicitly approve the exact candidate SHA after reviewing the evidence and comparing it with the currently working `main` deployment. Until that instruction exists, `main` remains unchanged.

## Integrated `dev` baseline

The current integrated baseline is `dev@b492a71bd127063fb6444a24bbc3a7c764b33a31`.

Integrated deterministic evidence includes:

- control-plane startup, authenticated management boundary, migrations and durable SQLite state;
- sticky per-connection fail-closed egress resolution and secret redaction;
- deterministic mautrix-meta reconstruction from pinned upstream `ed37c9e6ce47e83dc75b9abea7b636302715b9bc`;
- account-aware login/messaging/media/E2EE proxy-context tests and direct-egress sentinels;
- Matrix -> Chatwoot behavior beginning at the normalized internal Matrix event boundary;
- signed Chatwoot -> Matrix routing, attachments, provenance, duplicate suppression and ambiguous-response reconciliation;
- two-tenant A/B routing/egress, restart persistence, assigned-proxy failure and secret-canary checks;
- cold backup/restore of the full control-plane data volume in a disposable environment;
- pre-Meta provisioning claims: one-time digest-only authority, exact connection binding, bootstrap egress before provider traffic, login-ID binding and direct-sentinel proof.

These facts are strong engineering evidence. They are not equivalent to the global Definition of Done.

## Current implementation branch: real Matrix/Synapse ingestion

`feature/matrix-synapse-ingestion` closes the major gap between the normalized internal Phase 4 boundary and actual Synapse Client-Server ingestion.

**Current status:** implemented on branch and undergoing exact-SHA acceptance. It is not integrated until a PR to `dev` and the resulting post-merge `dev` SHA are both green.

Implemented behavior includes:

- schema v4 `matrix_room_bindings` and `matrix_sync_checkpoints`;
- standard `/sync` client using a dedicated Matrix service identity;
- bridge-bot-only trusted invitation auto-join rather than global Application Service visibility;
- exact room attribution from bridge-authored `m.bridge`/`uk.half-shot.bridge` state: `channel.receiver -> mautrix_login_id -> meta_connection`, with `channel.id -> remote_thread_id`;
- strict persisted room/thread/login/tenant conflict rejection;
- bootstrap sync with timeline limit zero and durable `next_batch` checkpointing;
- limited-timeline/gap fail-closed behavior without checkpoint advancement;
- runtime long-poll loop, bounded backoff, shutdown handling and Matrix-aware readiness;
- fork metadata on every remote bridged message part carrying explicit Meta provenance and provider remote sender ID;
- ordinary Matrix and Chatwoot-originated events excluded from Meta inbound routing;
- text, image/video/audio/file metadata, encrypted-file metadata and Matrix voice-note semantics;
- encrypted Matrix media fails closed if its v2/JWK metadata is unusable, including a missing `decrypt` key operation, and the checkpoint is not advanced;
- a composed batch-retry regression proves that a retryable downstream Chatwoot failure leaves `next_batch` unchanged, replay deduplicates already-delivered earlier events, and only the failed event performs its side effect on recovery;
- real disposable Synapse acceptance covering authentication, trusted invite/join, two tenants/two rooms, same numeric remote contact across tenants, unbound-room rejection, text/PDF/voice-note normalization and checkpoint persistence after SQLite reopen;
- cold backup/restore acceptance now explicitly seeds and verifies `matrix_room_bindings` and `matrix_sync_checkpoints` after destruction of the original volume and restoration into a fresh volume;
- Validate, Phase 6 and Docker build the same Matrix-enabled fork variant.

The bootstrap policy intentionally begins ingestion from the first persisted `/sync` token with timeline limit zero. Historical messages that predate initial ingestion startup are not replayed automatically; adding backfill would require a separate explicit policy for historical CRM side effects.

The exact branch candidate must still finish green after the final documentation SHA; do not treat the above as integrated evidence yet.

## Remaining blocking gaps

### 1. Targeted Chatwoot tenancy contract completion

The normative Chatwoot tenancy contract still requires explicit coverage beyond the current A/B topology, including:

- colliding numeric Chatwoot account/inbox/conversation IDs across separate tenant contexts;
- webhook/binding mismatch rejection across tenants;
- tenant-isolated remote-contact identity;
- cross-binding conversation rejection;
- explicit semantics for changing an active inbox without silently rewriting historical conversation bindings.

Existing code may satisfy some of these properties. Missing evidence must be added; runtime changes should be made only if those tests expose defects.

**Status:** remaining deterministic technical/coverage blocker after Matrix ingestion integrates.

### 2. Real Meta/provider deployment evidence

Repository CI intentionally does not use real Facebook customer credentials or paid residential proxies. Therefore it cannot prove:

- one deployed patched mautrix process hosting at least two real Meta logins;
- real portal creation automatically invites the Matrix ingestion service principal at the expected lifecycle point;
- actual Meta acceptance for both sessions;
- actual public exit IP/country per account;
- absence of host/Contabo direct egress on the public network;
- real login/reconnect/messaging/media/avatar/E2EE behavior through assigned egress;
- real Meta -> Matrix -> Chatwoot -> Matrix -> Meta round trip;
- behavior through the actual Coolify/DNS/TLS/network policy.

**Status:** mandatory controlled staging/human evidence. Test doubles cannot honestly replace it.

### 3. Production backup operations

The repository proves the recovery algorithm on disposable Docker volumes, now including Matrix room attribution and sync checkpoint state. It does not prove real backup storage, encryption/access control, retention, off-host durability, restore permissions or Coolify-specific volume identifiers.

**Status:** production-operations/human evidence gap.

## Non-blocking but material debt

- dynamic Instagram identity remains explicitly unsupported until its identity transition is modeled and tested;
- Messenger Lite dynamic egress remains unsupported because stable pre-network identity is unavailable in that flow;
- final E2EE public-exit evidence requires staging;
- request-scoped media transports may create transient connection/resource overhead under heavy chunked usage;
- container/action/package inputs are not fully hermetic or digest-pinned;
- the fork delta is composed from multiple deterministic Python applicators and should be consolidated after correctness is stable;
- production operator RBAC, retention policy, metrics/OpenTelemetry and PostgreSQL migration remain deferred according to architecture docs.

## Completion sequence from current state

1. finish exact-SHA Matrix/Synapse ingestion acceptance and all inherited regressions;
2. open a PR only to `dev`, require all PR checks including Phase 3 topology, merge with exact-head guard, then require post-merge `dev` checks green;
3. create a focused branch from that green `dev` and close the remaining Chatwoot tenancy assertions with tests first and code fixes only where required;
4. rerun the complete repository acceptance matrix on one exact resulting `dev` SHA;
5. prepare a staging checklist for every assertion CI cannot make;
6. human operator performs controlled staging with at least two disposable real Meta accounts and real egress endpoints;
7. perform the real off-host backup/restore operational drill and capture evidence;
8. human owner evaluates the exact `dev` candidate against the currently working `main` deployment;
9. only an explicit human instruction approving the exact SHA may authorize any later `main` promotion.

## Current readiness statement

The repository is **not production-ready yet** under its own normative documents.

Provisioning is integrated in `dev`. Real Matrix/Synapse ingestion is implemented on the current feature branch but still requires final exact-SHA acceptance and integration. After that, targeted Chatwoot tenancy coverage, real Meta/provider staging and real production-operations evidence remain outstanding.

No CI result or merge to `dev` changes the human-controlled `main` promotion rule.
