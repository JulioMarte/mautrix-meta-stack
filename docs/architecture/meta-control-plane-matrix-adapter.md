# Meta Control Plane Matrix Adapter Contract

Status: normative for `feature/meta-control-plane`

## Purpose

The control plane needs a deterministic way to consume Matrix events, attribute each bridged room to exactly one `meta_connection`, and send outbound replies back to the correct room without making assumptions from display names or room aliases.

## Adapter role

The Matrix adapter is infrastructure owned by the control plane. It is not a second bridge and MUST NOT implement Meta protocol/session behavior.

Responsibilities:

- authenticate to Synapse using a dedicated service identity/credential;
- receive relevant room events;
- discover/persist room-to-connection binding using stable identifiers;
- normalize supported events;
- submit outbound events to the exact bound room;
- maintain a durable sync/checkpoint position if the selected ingestion mechanism requires one;
- provide provenance for loop suppression.

## Ingestion mechanism decision

The implementation MUST choose and document one concrete mechanism before Phase 4 is declared complete:

1. Matrix Client-Server sync/sliding-sync style consumption using a dedicated service account; or
2. an Application Service/event push mechanism if it can be scoped and operated safely.

The first implementation SHOULD prefer the least invasive mechanism that can be tested deterministically with our Synapse deployment. The code MUST hide the selected transport behind an adapter boundary so the domain event contract does not depend on `/sync` response structure.

## Service identity

Do not use a human Matrix account as the long-lived control-plane credential in production.

The deployment MUST provision or configure a dedicated service principal with only the access needed for rooms it processes. Its access token is a secret and MUST never be logged or rendered.

## Room attribution

A Matrix room MUST NOT be routed merely because its name resembles a Facebook thread.

The control plane needs a persisted, tenant-scoped association between:

```text
matrix_room_id
meta_connection_id
remote_thread_id when available
```

Room discovery may use bridge-provided stable metadata/state/events where available, but the exact signal used MUST be documented and contract-tested against the pinned mautrix version before Phase 4 is accepted.

If the adapter cannot unambiguously associate a room with exactly one active `meta_connection`, it MUST not forward the event to Chatwoot.

## Initial room discovery/reconciliation

The adapter MUST support both:

- a new bridged room appearing after the control plane is already running;
- restart/redeployment where rooms already exist.

Startup reconciliation MUST rebuild in-memory caches only from persisted authoritative bindings plus verifiable Matrix/bridge metadata. It must not create duplicate Chatwoot conversations merely because a room was rediscovered.

## Checkpoint and delivery semantics

If using a pull/sync API, the sync token/checkpoint MUST be persisted so restart does not cause uncontrolled replay. Replayed events are still handled through `processed_events` idempotency; the checkpoint alone is not the duplicate-prevention mechanism.

An event should be considered consumed from the Matrix transport only after enough durable local state exists to recover processing safely after a crash.

## Supported event scope

Phase 4 initially needs at least:

- text messages;
- one representative attachment type;
- sender identity sufficient for Chatwoot contact attribution;
- reply/correlation metadata where available and required by the chosen UX.

Edits, reactions, read receipts, typing indicators and redactions may be deferred unless explicitly added to the branch Definition of Done.

Unsupported event types MUST be ignored with structured diagnostics or recorded as unsupported; they must not be silently converted to text.

## Outbound sends

For Chatwoot -> Matrix, the adapter MUST send to the exact persisted `matrix_room_id` associated with the target `conversation_binding`.

Each outbound message MUST carry or persist provenance/correlation sufficient for the inbound Matrix path to recognize its own reflected event and suppress a loop.

## Authorization boundary

A room event from tenant A may never resolve a binding belonging to tenant B. All room lookups are scoped by the connection/tenant association, and ambiguous/global lookups are forbidden.

## Recovery behavior

Synapse unavailable -> retain retryable outbound state and do not mark delivery complete.

Sync token rejected/invalid -> perform bounded reconciliation/backfill according to Matrix semantics, while relying on event idempotency to avoid duplicate Chatwoot effects.

Room missing/left -> mark routing degraded/operator-action-required; do not create an arbitrary replacement room.

## Required CI proofs

CI MUST run a real disposable Synapse and prove:

- service authentication works and unauthorized calls fail;
- two tenant connections produce/use distinct Matrix rooms;
- room attribution resolves to the correct `meta_connection`;
- ambiguous/unbound rooms do not reach Chatwoot;
- restart preserves checkpoint/binding behavior without duplicate side effects;
- one representative inbound text event and attachment normalize correctly;
- an outbound Chatwoot-originated Matrix event is recognized as our own provenance and is not echoed back;
- tenant A room events never route through tenant B bindings.