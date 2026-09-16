# History and Meta safety guardrails

## Goal

Keep historical Chatwoot synchronization useful without allowing an operator setting or a reconnect to trigger an unbounded Meta thread crawl.

## Two different history paths

The product has two separate mechanisms that must not be confused:

1. **Meta thread rediscovery (network-facing).** mautrix-meta asks Facebook/Messenger for additional pages of threads. This is remote traffic and is rate-limited by the runtime wrapper.
2. **Chatwoot history window (local).** The integration reads already-bridged events from Synapse/Matrix and mirrors unseen events into Chatwoot. Changing this window does not itself fetch arbitrary years from Meta.

## Hard product history limit

`history_import_days` has a hard maximum of **30 days**. Values above 30 are rejected by the backend, and previously persisted values above the limit are clamped to 30 at startup.

The 30-day window is intentionally bounded. The integration no longer materializes an otherwise-old portal by reaching outside the selected window for a single all-time “latest customer message”. A portal appears in Chatwoot when it has an importable event inside the selected window or receives a new live event.

## Natural window expansion

Changing the history window from a smaller value to a larger value, for example **5 -> 30 days**, does not require deleting Chatwoot or Matrix state.

After the setting is saved, the integration requests an authoritative portal reconcile. Each portal is scanned using the new local Matrix cutoff. Existing `processed_events` rows remain the dedupe boundary, so only Matrix events that were previously outside the window and are now inside it are mirrored. Existing room links and Chatwoot conversations are preserved.

Shrinking the window does not delete messages that were already mirrored to Chatwoot.

## Meta thread rediscovery bounds

The deployed mautrix-meta runtime never permits an unlimited `thread_backfill.batch_count`.

At startup it queries the same authenticated internal proxy resolver used by mautrix-meta and applies one of two conservative profiles:

- **DIRECT / resolver unavailable:** at most 2 thread-list pages per process start, with 20 seconds between pages.
- **Active configured proxy:** at most 5 thread-list pages per process start, with 10 seconds between pages.

If the resolver cannot be reached, the runtime fails conservative and uses the DIRECT profile.

An active proxy is only a routing signal. The stack does **not** claim to detect or certify that a proxy is residential, and proxy presence does not disable the hard limits.

## Why the two controls are separate

A large Chatwoot history window is a local data-volume concern. Thread rediscovery is a Meta-network request-volume concern. Tying the local Matrix cutoff directly to remote Facebook fetch depth would create false safety assumptions and could make ordinary Chatwoot reindexing unexpectedly generate remote traffic.

## Operator expectations

- Default history: 30 days.
- Maximum history: 30 days.
- 0 days disables historical import.
- Increasing the window automatically re-runs local portal reconciliation.
- No all-time fallback message is imported outside the selected window.
- Meta thread discovery remains bounded even after restart/reconnect.
