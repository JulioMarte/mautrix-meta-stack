# Production Readiness Ledger

Status: authoritative current-state ledger for the Meta Control Plane workstream.

This document prevents green phase CI from being mistaken for end-to-end production readiness. It records what is proven, what is implemented but not integrated, and what still requires staging or human evidence.

## Promotion rule

`main` is the current human-accepted deployment baseline. No automated agent, bot, CI workflow or unattended release process may promote `dev` to `main`.

A human owner must explicitly approve the exact candidate SHA after reviewing the evidence and comparing it with the currently working `main` deployment. Until that instruction exists, `main` remains unchanged.

## Integrated `dev` baseline

The current integrated baseline is `dev@af129d133aa863f0818b7ec96c0cb9524b90a378`.

Integrated deterministic evidence includes:

- control-plane startup, authenticated management boundary, migrations and durable SQLite state;
- sticky per-connection fail-closed egress resolution and secret redaction;
- deterministic mautrix-meta reconstruction from pinned upstream `ed37c9e6ce47e83dc75b9abea7b636302715b9bc`;
- account-aware login/messaging/media/E2EE proxy-context tests and direct-egress sentinels;
- signed Chatwoot -> Matrix routing, attachments, provenance, duplicate suppression and ambiguous-response reconciliation;
- two-tenant A/B routing/egress, restart persistence, assigned-proxy failure and secret-canary checks;
- cold backup/restore of the full control-plane data volume in a disposable environment;
- pre-Meta provisioning claims: one-time digest-only authority, exact connection binding, bootstrap egress before provider traffic, login-ID binding and direct-sentinel proof;
- real Matrix/Synapse Client-Server ingestion using a dedicated Matrix service identity, verified bridge room attribution, durable sync checkpoints and bounded gap recovery;
- disposable Synapse acceptance with two tenants/two rooms, trusted invite/join, unbound-room rejection, text/PDF/voice-note normalization and checkpoint persistence;
- backup/restore coverage for Matrix room attribution and sync checkpoint state.

These facts are strong engineering evidence. They are not equivalent to the global Definition of Done.

## Integrated Matrix/Synapse ingestion

PR #17 is merged to `dev@af129d133aa863f0818b7ec96c0cb9524b90a378`. The post-merge workflow set has no failed runs.

Integrated behavior includes:

- schema v4 `matrix_room_bindings` and `matrix_sync_checkpoints`;
- standard `/sync` client using a dedicated Matrix service identity;
- bridge-bot-only trusted invitation auto-join rather than global Application Service visibility;
- exact room attribution from bridge-authored `m.bridge`/`uk.half-shot.bridge` state: `channel.receiver -> mautrix_login_id -> meta_connection`, with `channel.id -> remote_thread_id`;
- strict persisted room/thread/login/tenant conflict rejection;
- unknown/inactive bridge logins treated as unattributable rooms with zero downstream side effects, without allowing one invalid room to block unrelated valid rooms in the same sync batch;
- bootstrap sync with timeline limit zero and durable `next_batch` checkpointing;
- bounded recovery of `limited` timelines using forward `/messages` pagination from the persisted checkpoint to `prev_batch`, with event-ID deduplication across recovered pages and current timeline;
- recovery failure, missing range information, non-converging pagination or oversized history leaves the checkpoint unchanged;
- runtime long-poll loop, bounded backoff, shutdown handling and Matrix-aware readiness;
- fork metadata on bridged message parts carrying explicit Meta provenance and provider remote sender ID;
- ordinary Matrix and Chatwoot-originated events excluded from Meta inbound routing;
- text, image/video/audio/file metadata, encrypted-file metadata and Matrix voice-note semantics;
- encrypted Matrix media fails closed if its v2/JWK metadata is unusable, with the checkpoint left unchanged;
- composed retry proof: downstream Chatwoot failure leaves `next_batch` unchanged, replay deduplicates already-delivered events and only the failed event repeats its side effect;
- the same Matrix-enabled fork variant is exercised by Validate, Phase 6 and Docker builds.

The bootstrap policy intentionally starts at the first persisted `/sync` token with timeline limit zero. Historical messages predating initial ingestion startup are not replayed automatically. Historical CRM backfill would require a separate explicit policy.

## Current implementation branch: Chatwoot tenancy completion

`feature/chatwoot-tenancy-completion` closes the remaining deterministic Chatwoot tenancy/migration gap.

**Current status:** implementation and focused tests exist on the branch; it is not integrated until an exact-head PR to `dev` and the resulting post-merge `dev` SHA are green.

Implemented/proven behavior includes:

- colliding account/inbox/conversation/contact-looking IDs across different tenant contexts do not cross-route;
- deterministic remote-contact identity remains scoped by tenant + Meta connection + remote contact;
- webhook/binding mismatches fail closed before Matrix side effects;
- a connection migration from Chatwoot binding A to B preserves historical thread routing through A while new threads use B;
- Chatwoot replies arriving through historical binding A still target the original Matrix room after the connection points to B;
- binding B cannot claim a historical A conversation merely because numeric conversation IDs collide;
- another tenant with colliding numeric IDs cannot claim the conversation;
- if historical binding A is disabled, new delivery for that historical thread fails terminally with no Chatwoot side effect and no silent fallback to B;
- a dedicated `Chatwoot tenancy acceptance` workflow runs the focused contract alongside broader Phase 4/5/6 regressions.

The current MVP deliberately supports at most one `(account_id, inbox_id)` binding per tenant. Simultaneously configuring two different Chatwoot installations with the same numeric account+inbox IDs inside one tenant is outside the MVP and would require promoting installation/binding identity into the persistent conversation key.

## Remaining blocking gaps

### 1. Chatwoot tenancy integration evidence

The deterministic tenancy gap is implemented on `feature/chatwoot-tenancy-completion`, but it is not integrated evidence until:

- the final documentation SHA passes `Chatwoot tenancy acceptance` and inherited regressions;
- a PR targets `dev` and passes exact-head checks;
- the PR is merged only to `dev` with head-SHA protection;
- the resulting `dev` merge SHA passes the post-merge acceptance matrix.

**Status:** final deterministic integration blocker.

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

The repository proves the recovery algorithm on disposable Docker volumes, including Matrix room attribution and sync checkpoint state. It does not prove real backup storage, encryption/access control, retention, off-host durability, restore permissions or Coolify-specific volume identifiers.

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

1. finish exact-SHA Chatwoot tenancy acceptance and inherited regressions;
2. open a PR only to `dev`, require all exact-head checks including Phase 3/4/5/6, provisioning, Matrix ingestion, recovery and tenancy, then merge with an exact-head guard;
3. require the complete repository acceptance matrix green on the resulting `dev` SHA;
4. prepare a staging checklist for every assertion CI cannot make;
5. human operator performs controlled staging with at least two disposable real Meta accounts and two real egress endpoints;
6. verify real public exit IP/country, reconnect stickiness, provider media/avatar/E2EE behavior and the complete Meta -> Matrix -> Chatwoot -> Matrix -> Meta round trip;
7. perform the real off-host backup/restore operational drill and capture evidence for encryption/access/retention/restore permissions and actual volume identifiers;
8. human owner evaluates the exact `dev` candidate against the currently working `main` deployment;
9. only an explicit human instruction approving the exact SHA may authorize any later `main` promotion.

## Current readiness statement

The repository is **not production-ready yet** under its own normative documents.

Provisioning and real Matrix/Synapse ingestion are integrated in `dev`. The final deterministic Chatwoot tenancy/migration blocker is implemented on `feature/chatwoot-tenancy-completion` but still requires exact-head integration evidence. After that, the remaining mandatory blockers are controlled real-provider staging and real production backup/deployment operations.

No CI result or merge to `dev` changes the human-controlled `main` promotion rule.
