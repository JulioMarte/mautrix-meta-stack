# Production Readiness Ledger

Status: authoritative current-state ledger for the Meta Control Plane workstream.

This document prevents green phase CI from being mistaken for end-to-end production readiness. It records what is proven, what is integrated, and what still requires controlled staging or human evidence.

## Promotion rule

`main` is the current human-accepted deployment baseline. No automated agent, bot, CI workflow or unattended release process may promote `dev` to `main`.

A human owner must explicitly approve the exact candidate SHA after reviewing the evidence and comparing it with the currently working `main` deployment. Until that instruction exists, `main` remains unchanged.

## Integrated `dev` baseline

The current integrated baseline is `dev@1b7263daac18a9d83e02c8b22b44b510e0c10aea`.

This baseline includes PR #18 (Chatwoot tenancy/migration completion) and PR #19 (exclusive live egress reservations). The post-merge push matrix for this exact SHA contains no failed workflow run.

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
- backup/restore coverage for Matrix room attribution and sync checkpoint state;
- Chatwoot historical-route preservation across binding migration, current-binding selection for new threads, authenticated historical webhook routing, cross-tenant/cross-binding collision rejection and fail-closed disabled-history behavior;
- exclusive live egress ownership: a non-disabled connection reserves its profile, disabling releases it, and stale reactivation fails when another live connection has legitimately reused that profile.

These facts are strong engineering evidence. They are not equivalent to the global Definition of Done.

## Integrated Matrix/Synapse ingestion

PR #17 is integrated in `dev`.

Integrated behavior includes:

- schema-backed `matrix_room_bindings` and `matrix_sync_checkpoints`;
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
- composed retry proof: downstream Chatwoot failure leaves `next_batch` unchanged, replay deduplicates already-delivered events and only the failed event repeats its side effect.

The bootstrap policy intentionally starts at the first persisted `/sync` token with timeline limit zero. Historical messages predating initial ingestion startup are not replayed automatically. Historical CRM backfill would require a separate explicit policy.

## Integrated Chatwoot tenancy and migration semantics

PR #18 is integrated in `dev`.

Integrated/proven behavior includes:

- colliding account/inbox/conversation/contact-looking IDs across different tenant contexts do not cross-route;
- deterministic remote-contact identity remains scoped by tenant + Meta connection + remote contact;
- webhook/binding mismatches fail closed before Matrix side effects;
- a connection migration from Chatwoot binding A to B preserves historical thread routing through A while new threads use B;
- Chatwoot replies arriving through historical binding A still target the original Matrix room after the connection points at B;
- binding B cannot claim a historical A conversation merely because numeric conversation IDs collide;
- another tenant with colliding numeric IDs cannot claim the conversation;
- if historical binding A is disabled, new delivery for that historical thread fails terminally with no Chatwoot side effect and no silent fallback to B;
- dedicated Chatwoot tenancy acceptance runs alongside broader Phase 4/5/6 regressions.

The current MVP deliberately supports at most one `(account_id, inbox_id)` binding per tenant. Simultaneously configuring two different Chatwoot installations with the same numeric account+inbox IDs inside one tenant remains outside the MVP and would require promoting installation/binding identity into the persistent conversation key.

## Integrated egress ownership semantics

PR #19 is integrated in `dev`.

The global `egress_profiles` inventory remains operator-managed, but one live reservation is enforced per profile. Connections in `draft`, `ready`, `active`, `degraded` or `blocked` reserve their assigned profile; `disabled` releases it.

Deterministic coverage proves:

- two live connections cannot share the same profile;
- a disabled connection releases the profile for deliberate reuse;
- a disabled connection cannot reactivate if another live connection has reused its stale assignment;
- distinct A/B profiles continue to support independent multi-tenant routing;
- the behavior is enforced at the persistence boundary rather than relying only on operator convention.

## Remaining blocking gaps

### 1. Real Meta/provider deployment evidence

Repository CI intentionally does not use real Facebook customer credentials or paid residential proxies. Therefore it cannot prove:

- one deployed patched mautrix process hosting at least two real Meta logins;
- actual Meta acceptance for both sessions;
- actual public exit IP/country per account;
- absence of host/Contabo direct egress on the public network;
- real login/reconnect/messaging/media/avatar/E2EE behavior through assigned egress;
- real Meta -> Matrix -> Chatwoot -> Matrix -> Meta round trip;
- behavior through the actual Coolify/DNS/TLS/network policy;
- the real portal lifecycle behavior needed by the Matrix ingestion identity in the deployed environment.

**Status:** mandatory controlled staging/human evidence. Test doubles cannot honestly replace it.

The normative procedure is `staging-acceptance-runbook.md`. All mandatory checks must pass against one exact candidate SHA; evidence from different SHAs must not be combined into one acceptance result.

### 2. Production backup operations

The repository proves the recovery algorithm on disposable Docker volumes, including Matrix room attribution and sync checkpoint state. It does not prove real backup storage, encryption/access control, retention, off-host durability, restore permissions or Coolify-specific volume identifiers.

**Status:** production-operations/human evidence gap.

`staging-acceptance-runbook.md` requires an off-volume backup and destructive restore drill using the intended operational mechanism before readiness can be claimed.

## Non-blocking but material debt

- dynamic Instagram identity remains explicitly unsupported until its identity transition is modeled and tested;
- Messenger Lite dynamic egress remains unsupported because stable pre-network identity is unavailable in that flow;
- request-scoped media transports may create transient connection/resource overhead under heavy chunked usage;
- container/action/package inputs are not fully hermetic or digest-pinned;
- the fork delta is composed from multiple deterministic Python applicators and should be consolidated after correctness is stable;
- production operator RBAC, retention policy, metrics/OpenTelemetry and PostgreSQL migration remain deferred according to architecture docs;
- the server-rendered administrative surface remains intentionally minimal and should be expanded before broad operator self-service is claimed.

## Completion sequence from current state

1. select and record an exact green `dev` candidate SHA for staging;
2. execute every mandatory check in `staging-acceptance-runbook.md` with at least two disposable real Meta accounts and two distinct real egress endpoints;
3. verify real public exit IP/country, reconnect stickiness, provider media/avatar/E2EE behavior and the complete Meta -> Matrix -> Chatwoot -> Matrix -> Meta round trip;
4. perform the real off-host backup/restore operational drill and capture evidence for encryption/access/retention/restore permissions and actual volume identifiers;
5. record any failure without rewriting or combining evidence across candidate SHAs; fix on a normal branch and rerun against the new exact candidate;
6. human owner evaluates the exact passing `dev` candidate against the currently working `main` deployment;
7. only an explicit human instruction approving that exact SHA may authorize any later `main` promotion.

## Current readiness statement

The repository is **not production-ready yet** under its own normative documents.

The deterministic repository blockers identified earlier—provisioning, real Matrix/Synapse ingestion, Chatwoot tenancy/migration semantics and exclusive live egress ownership—are integrated in `dev`. The remaining mandatory blockers are controlled real-provider/Coolify staging evidence and real production backup/deployment operations.

No CI result or merge to `dev` changes the human-controlled `main` promotion rule.