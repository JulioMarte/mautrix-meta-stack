# Meta Control Plane Matrix Adapter Contract

Status: normative for `dev` integration

## Purpose

The control plane consumes Matrix events from Synapse, attributes each mautrix-meta portal room to exactly one `meta_connection`, and feeds the existing Matrix -> Chatwoot domain service without trusting room names, aliases, display names or caller-supplied connection identifiers.

## Ingestion mechanism

The selected mechanism is the standard Matrix Client-Server `/sync` API with a dedicated service principal. The adapter is infrastructure owned by the control plane; it is not a second Meta bridge and does not implement Meta sessions or protocol behavior.

The runtime is opt-in through `MATRIX_SYNC_ENABLED=true`. When disabled, existing control-plane health and behavior remain unchanged. When enabled, Matrix ingestion readiness is part of `/health/ready`.

## Service identity and portal membership

The deployment provisions a dedicated Matrix service principal and access token for ingestion. It MUST NOT be a human account in production. The token is secret and MUST never be logged, persisted in SQLite or rendered through management APIs.

The mautrix-meta fork accepts `matrix_ingestor_mxid`. When configured, the bridge bot reconciles that MXID as an invitee of portal rooms owned by the bridge. The ingestion client does not auto-join arbitrary invitations: it joins a room only when the invite state contains exactly one `m.room.member` invitation for its own MXID sent by the exact configured mautrix bridge bot. Invitations from any other sender are ignored.

This design deliberately avoids a second Application Service with a global namespace.

## Room attribution

A room is attributable only from verifiable bridge state produced by the configured bridge bot. The adapter accepts `m.bridge` or `uk.half-shot.bridge` state when all of the following are true:

- event sender equals the configured bridge bot MXID;
- `protocol.id` is in the configured allowlist (initially `facebookgo`);
- `channel.id` is a non-empty remote thread ID;
- `channel.receiver` is a non-empty mautrix `UserLoginID`;
- that `UserLoginID` resolves to exactly one active `meta_connection`.

The resulting association is:

```text
matrix_room_id
  -> m.bridge channel.receiver
  -> meta_connections.mautrix_login_id
  -> meta_connection_id + tenant_id

m.bridge channel.id -> remote_thread_id
```

A room whose bridge state points to an unknown or inactive login is **unattributable**: it MUST produce no Chatwoot side effect and MUST NOT create a room binding, but it MUST NOT block unrelated valid rooms in the same `/sync` batch. Ambiguous identity, contradictory bridge state, conflicting persisted binding or cross-tenant inconsistency remains a hard fail-closed error for the batch and MUST NOT reach Chatwoot.

## Persistent room authority

Schema v4 adds `matrix_room_bindings`, separate from `conversation_bindings`.

`matrix_room_bindings` exists before a Chatwoot conversation is created and is the Matrix-side routing authority. `conversation_bindings` remains the later CRM/provider conversation association.

A Matrix room may bind to only one Meta connection. A `(meta_connection_id, remote_thread_id)` may bind to only one Matrix room. Re-observing the same verified association is idempotent; observing a contradictory association is a conflict, not an implicit update.

## Checkpoint and replay semantics

Schema v4 also adds `matrix_sync_checkpoints`. The persisted `/sync` `next_batch` token is transport progress, not event idempotency. `processed_events` remains the authority preventing duplicate external side effects.

On first bootstrap the adapter requests timeline limit `0`, verifies/reconciles joined rooms, accepts only trusted bridge invitations, persists the resulting checkpoint and does not replay old timeline messages.

Subsequent syncs use a bounded timeline. When Synapse marks a room timeline `limited`, the adapter MUST recover the missing range before processing the current timeline by calling `/rooms/{roomId}/messages` in forward direction from the previously persisted `/sync` checkpoint to the timeline `prev_batch`. Recovery is bounded by configured page and event limits. Events repeated across recovery pages or between recovered history and the current timeline are deduplicated by Matrix event ID while preserving first-seen order.

A gap is not silently skipped. Missing recovery capability, a missing `prev_batch`, non-converging pagination, an oversized history window or any recovery request failure MUST abort the batch before downstream side effects for that room and MUST leave the persisted checkpoint unchanged. The next run therefore retries from the same transport position; downstream `processed_events` idempotency protects side effects already completed earlier in a partially processed batch.

The checkpoint is written only after the batch has been processed successfully enough to recover safely after restart.

## Message provenance and sender identity

A Matrix MXID is not a stable Meta remote-user identifier. The fork therefore decorates every bridged remote message part before it reaches Matrix with namespaced metadata:

```text
com.mautrix_meta_stack.provenance = { source: "meta" }
com.mautrix_meta_stack.remote_sender_id = <provider remote user id>
```

The adapter processes an inbound event only when provenance is explicitly `meta`. Ordinary Matrix messages and Chatwoot-originated echoes are ignored. A Meta-provenance event without a remote sender ID fails closed and does not advance the checkpoint.

This avoids parsing ghost MXIDs or depending on username templates.

## Supported event scope

The current ingestion path normalizes:

- `m.text`;
- `m.image`;
- `m.video`;
- `m.audio`;
- `m.file`;
- Matrix encrypted-file v2 metadata used by the existing media downloader;
- `org.matrix.msc3245.voice` on `m.audio`, preserved as `Attachment.voiceNote=true`.

Unsupported event types are ignored rather than coerced to text. Downstream Matrix -> Chatwoot media download/decryption/upload remains covered by the Phase 4 media gates.

## Runtime behavior and readiness

`HttpMatrixSyncClient` uses a validated HTTP(S) base URL, bearer authorization header, bounded request timeout, bounded response size and redirect refusal.

`MatrixSyncRunner` performs continuous long polling with bounded exponential backoff after failures and clean `AbortSignal` shutdown.

When ingestion is enabled:

- readiness begins false until at least one sync completes successfully;
- a later sync failure makes `/health/ready` return 503 with `MATRIX_SYNC_NOT_READY`;
- the specific transport error remains in structured logs and is not exposed in the readiness response;
- liveness remains independent so a temporarily unavailable Synapse does not falsely imply process death.

## Outbound sends and loop suppression

Chatwoot -> Matrix continues to send to the exact persisted `matrix_room_id` from `conversation_bindings` and marks outbound events with Chatwoot provenance. The Synapse ingestion path only accepts explicit Meta provenance, so reflected Chatwoot events cannot loop back to Chatwoot.

## Required CI proofs

The Matrix ingestion acceptance gate MUST use a disposable real Synapse and prove:

- valid service authentication succeeds and an invalid token is rejected;
- trusted bridge-bot invitations auto-join while untrusted invitations do not;
- two active tenant connections with distinct `mautrix_login_id` values bind to distinct rooms;
- the same numeric remote contact ID in tenant A and tenant B routes to the correct distinct connection;
- an invited room whose `channel.receiver` does not resolve to an active connection produces no downstream delivery, no persisted room binding and does not prevent unrelated valid rooms from advancing the checkpoint;
- text, PDF/file metadata and a Matrix voice note normalize correctly;
- ordinary Matrix messages do not block the checkpoint or reach Chatwoot;
- room bindings and sync checkpoint survive SQLite close/reopen;
- malformed/ambiguous/conflicting attribution fails closed;
- bounded gap recovery has deterministic tests proving multi-page recovery, chronological composition with the current timeline, event-ID overlap deduplication, and unchanged checkpoint on unavailable, non-converging or oversized recovery;
- the final fork variant used by Docker, Validate and Phase 6 contains the same Matrix metadata and membership patches.

The live Synapse gate may use a fake downstream `MatrixToChatwootService` to isolate the Client-Server boundary because the downstream HTTP/media behavior is already covered by Phase 4. It MUST NOT replace the real Synapse with a mocked `/sync` response for this acceptance proof.

## Staging boundary

CI proves Client-Server semantics and the deterministic fork patches. It does not prove that a real Meta-created portal in a deployed environment invited the service principal at the expected time. That final portal lifecycle proof is part of staging with the real patched mautrix-meta process and real Meta sessions.
