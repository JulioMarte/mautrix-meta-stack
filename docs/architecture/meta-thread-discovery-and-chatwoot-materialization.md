# Meta thread discovery and Chatwoot materialization

## Problem

The product contract is stricter than the default behavior of either upstream component: after a Facebook account is connected, existing Messenger/Marketplace conversations should become visible in Chatwoot without requiring a new inbound message and without requiring an operator to open Element.

Two independent state machines can prevent that result.

1. mautrix-meta persists `UserLoginMetadata.BackfillCompleted`. Upstream `StartThreadBackfill` skips all thread pagination when that flag is already true. A restored/reused login can therefore connect successfully and process the current messages page while never re-walking older thread pages in that process.
2. The integration reconciler historically created a Chatwoot room mapping only as a side effect of importing an inbound Matrix message. A verified portal with no recent inbound text remained absent from Chatwoot even though the Matrix room already existed.

## Product behavior on dev

The fork now treats thread discovery as a process-level reconciliation operation. The persisted upstream completion marker is retained, but it no longer suppresses discovery forever. On the first successful Meta socket connection in each bridge process, `StartThreadBackfill` is allowed to paginate again. An atomic guard prevents duplicate concurrent runs. If pagination fails, the guard is released so a later reconnect can retry. Successful pagination still runs at most once per process.

This intentionally trades additional Meta reads after a bridge restart for correctness. With `thread_backfill.batch_count: -1`, a process restart can walk the full thread history again; `batch_delay` remains the rate-limit control. Operators should monitor large accounts because the cost scales with the number of thread pages.

The integration reconciler now distinguishes chat portals from Matrix spaces. A verified `m.space` (including the Marketplace folder space) is never materialized as a Chatwoot conversation. For a verified joined chat portal, recent history is imported first. If no inbound event creates a mapping, the reconciler resolves a remote contact from authoritative Matrix membership state using only exclusive mautrix appservice ghost identities and creates the Chatwoot contact/conversation mapping directly. A trusted bridge-info `creator` is used only as a fallback and only when it also matches the exclusive appservice namespace.

## Security boundaries

No room is materialized merely because it has a familiar name or user-controlled `bridgebot` field. Portal provenance still requires trusted bridge state/invites from the installed mautrix appservice. Contact identity must match an exclusive appservice user namespace. The integration admin account, bridge bot, arbitrary local users, and Marketplace spaces are rejected as Chatwoot contact identities.

## Observability

The periodic reconciliation result now records:

- `meta_portal_reconcile_verified`
- `meta_portal_reconcile_linked`
- `meta_portal_reconcile_materialized`
- `meta_portal_reconcile_history`
- `meta_portal_reconcile_spaces`
- `meta_portal_reconcile_missing_contact`
- `meta_portal_reconcile_error`

Bridge logs distinguish normal first-run backfill from a process-start refresh of a previously completed backfill.

## Acceptance criteria

A deployment is acceptable when a connected Facebook account can restart with `BackfillCompleted=true`, logs one process-level thread refresh, creates Matrix portals for the rediscovered threads, auto-joins verified portals, skips the Marketplace space, and creates Chatwoot conversations for verified chat portals even when their recent Matrix history contains no inbound text event.
