# Meta thread discovery and Chatwoot linking

## Problem

The product contract has two separate requirements after a Facebook account is connected:

1. mautrix-meta must rediscover the Messenger/Marketplace thread set reliably;
2. Chatwoot must receive actual customer-message history for each usable portal without creating misleading empty conversations.

These requirements must not be conflated. Thread discovery belongs to mautrix-meta. Chatwoot conversation creation belongs to the Matrix ingestion layer and should remain driven by an importable customer message.

## Historical baseline

The last known-good pre-admin-cookie baseline is commit `8d8197a6dfe74b6dd2f27b1f62dffecf384b0715`. That baseline already had the authoritative portal reconciler and history import path, but it did **not** create a Chatwoot conversation merely because a verified Matrix portal existed. A room became linked when `import_recent_history()` or a live Matrix message successfully delivered a customer message.

PR #59 introduced browser-cookie onboarding in the admin UI. It changed onboarding/provisioning/UI code; it did not replace the Matrix history ingestion algorithm. Therefore the cookie-login button itself is not evidence of a message-sync regression.

## Thread rediscovery on dev

The fork keeps the useful thread-refresh correction introduced later. mautrix-meta persists `UserLoginMetadata.BackfillCompleted`, and upstream can otherwise skip thread pagination forever after that flag is set. On the first successful Meta socket connection in each bridge process, `StartThreadBackfill` is allowed to paginate again. An atomic guard prevents duplicate concurrent runs, and failure releases the guard so a reconnect may retry.

This is intentionally independent from Chatwoot linking. Rediscovery may create or recover Matrix portals, but it does not authorize empty Chatwoot conversations.

## Chatwoot linking semantics

The reconciler first verifies portal provenance from authoritative Synapse state and skips `m.space` rooms such as the Marketplace folder space. For each verified joined chat portal it calls `import_recent_history()`.

If that import creates a room link, the portal is counted as linked. If no importable customer message exists and no link is created, the room remains unlinked and the reconciler records `empty_unlinked`. It must **not** synthesize a Chatwoot contact/conversation solely from ghost membership or bridge metadata.

This restores the pre-cookie baseline behavior and avoids the failure mode observed in September 2026 where direct portal materialization produced visible Chatwoot conversations with no messages even though Matrix reconciliation itself was healthy.

## Why a clean-state retest matters

`processed_events` is persistent integration state used for idempotency. Reusing an integration volume across repeated destructive Chatwoot/Matrix tests can make historical events appear already consumed even when the operator has deleted or recreated downstream state. Likewise mautrix-meta persists login/backfill metadata in its own volume.

For a regression comparison against the historical baseline, a clean-state test should remove the disposable test volumes for Synapse, mautrix-meta, integration data and Chatwoot together. A partial reset is not equivalent to a new installation because those state machines can then disagree about what has already been delivered.

This clean-state recommendation is for diagnosis and acceptance testing, not a substitute for code correctness.

## Observability

The periodic reconciliation result records:

- `meta_portal_reconcile_verified`
- `meta_portal_reconcile_linked`
- `meta_portal_reconcile_materialized` (kept as a compatibility metric and expected to remain `0`)
- `meta_portal_reconcile_history`
- `meta_portal_reconcile_spaces`
- `meta_portal_reconcile_empty_unlinked`
- `meta_portal_reconcile_error`

Bridge logs distinguish normal first-run backfill from a process-start refresh of a previously completed backfill. The integration logs explicitly report verified portals that are left unlinked because they have no importable customer history.

## Acceptance criteria

A deployment is acceptable when a fresh Facebook connection can rediscover the expected thread set, create Matrix portals, auto-join verified portals, skip the Marketplace space, import available customer-message history into Chatwoot, and avoid creating an empty Chatwoot conversation for a portal whose Matrix history yields no importable customer message.
