# Matrix Ingestion Implementation Discoveries

Status: operational record for the real Synapse ingestion workstream. Normative behavior lives in `meta-control-plane-matrix-adapter.md`.

## 2026-09-11 — an unattributable room must not deny service to unrelated tenants

The first regular-sync implementation treated `MATRIX_BRIDGE_CONNECTION_NOT_ACTIVE` differently from bootstrap. During bootstrap, bridge state whose `channel.receiver` did not resolve to an active connection was treated as unattributable. During later `/sync` batches, the same condition escaped from `verifyRoom()` and aborted the entire checkpoint.

A real disposable-Synapse test exposed the consequence: one deliberately unbound room (`login-does-not-exist`) prevented valid tenant A and tenant B rooms in the same batch from completing, even though the unknown room itself never had authority to route a message.

Implication: an unknown or inactive bridge login is a **non-route**, not a cross-tenant identity conflict. The room produces no downstream side effect and no persisted room binding, while unrelated valid rooms may continue and the checkpoint may advance. Ambiguous identity, contradictory bridge state, persisted binding conflicts and cross-tenant inconsistencies remain hard fail-closed errors for the batch.

## 2026-09-11 — `/sync` `limited` timelines require bounded recovery, not silent skip or permanent stall

A bounded `/sync` timeline can be marked `limited`, which means events between the previously persisted sync position and the visible timeline may be absent. Advancing `next_batch` without recovering that interval would silently lose Meta-originated CRM events. Permanently rejecting every limited timeline, however, can stall ingestion indefinitely under legitimate load.

The implemented policy recovers the missing range using Matrix `/rooms/{roomId}/messages` in forward direction from the prior persisted `/sync` checkpoint to the room timeline `prev_batch`. Recovery is bounded by page and event limits, deduplicates event-ID overlap across pages and the current timeline, and preserves first-seen order.

Implication: gap recovery is part of the checkpoint safety contract. Missing recovery capability, missing `prev_batch`, non-converging pagination, oversized history or recovery request failure leaves the checkpoint unchanged. Automated tests must prove successful multi-page recovery and overlap deduplication as well as these fail-closed boundaries; a test that only asserts the error path is insufficient evidence.

## 2026-09-11 — checkpoint progress and event idempotency are separate authorities

A downstream side effect can succeed for an early event and fail retryably for a later event in the same `/sync` batch. Advancing the transport checkpoint would lose the failed event; rolling back the already successful external side effect is not generally possible.

Implication: `matrix_sync_checkpoints.next_batch` advances only after batch processing succeeds, while `processed_events` suppresses duplicate external side effects when the same transport batch is replayed. Tests compose these two mechanisms: a partial downstream failure preserves the old checkpoint, and retry reuses that checkpoint while avoiding a second side effect for the already delivered event.
