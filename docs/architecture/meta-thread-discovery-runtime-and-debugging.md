# Meta thread discovery runtime and debugging

Status: operational contract for `dev`.

## Root cause found on 2026-09-16

The stack configured mautrix-meta with `network.thread_backfill.batch_count: -1` and enabled message backfill, but the Compose runtime still launched the unmodified upstream `dock.mau.dev/mautrix/meta:v26.08.1` image by default. The repository's process-level thread-refresh patch lived only under `mautrix-meta-fork/` and was built by a dedicated CI job; normal `compose.yaml` did not build or run that patched binary.

This created a false acceptance signal: CI proved that a patched fork compiled, not that the deployed stack executed it.

Upstream v0.2608.1 persists `UserLoginMetadata.BackfillCompleted`. On later process starts, `StartThreadBackfill` returns immediately when that marker is true, before calling `runThreadBackfill`. Therefore `batch_count: -1` means unlimited pages only when thread backfill actually runs; it does not override the persisted completion guard.

## Runtime contract

The normal `mautrix-meta` Compose service now builds `mautrix-meta-runtime/Dockerfile` from the exact upstream v0.2608.1 commit `001f276beca5b90dead1bbc1351e1036e3f966a7`.

The runtime delta is intentionally small:

- add one process-local atomic guard to `MetaClient`;
- allow a persisted-complete thread backfill to run once after each process start;
- prevent duplicate concurrent triggers;
- release the process-local guard on failure so a reconnect can retry.

The configuration/registration one-shot containers remain on the official v26.08.1 image. Runtime and generated configuration therefore target the same upstream release instead of mixing an old fork baseline with a newer generated config.

A candidate is not acceptable merely because `mautrix-meta-runtime/Dockerfile` builds. CI must also assert that the `mautrix-meta` service in `compose.yaml` references that Dockerfile.

## Expected mautrix-meta logs

On a fresh login, the bridge should log `Starting thread backfill`.

When a persisted login reconnects after a bridge/container restart, it should log `Re-running thread backfill once after process start despite persisted completion marker`.

The upstream pagination code then emits a completion reason with `batches_processed`, for example no more threads, no more pages, unchanged thread key, or batch limit. With `batch_count: -1`, the configured positive batch-limit branch must not be the reason for stopping.

If the process never emits either thread-backfill start line after Meta socket connection, the wrong runtime image or a connection-state problem is suspected.

## Integration-container diagnostics

`integration/meta_debug_observability.py` emits safe JSON lines prefixed with `META_DEBUG`. It never logs cookies, tokens, proxy credentials, message bodies, or raw Meta payloads.

Every diagnostic interval it emits an `inventory_summary` containing:

- Matrix membership counts;
- verified/unverified Meta portal counts;
- spaces skipped;
- linked and unlinked portal counts;
- rooms that still contain eligible unprocessed history;
- integration `room_links` and `processed_events` counts;
- effective history-import settings;
- selected non-secret mautrix config values including thread-backfill and message-backfill limits.

For each portal whose diagnostic state changed it emits `portal_state` with:

- room membership and verification result;
- Chatwoot link/conversation identifiers when present;
- number of recent Matrix events inspected;
- number of `m.room.message` events;
- messages excluded because they came from the integration admin or bridge bot;
- unsupported message types;
- empty bodies;
- already-processed events;
- eligible unprocessed text/notice events.

This separates the pipeline into observable boundaries:

1. no Matrix portal -> thread discovery / mautrix-meta problem;
2. Matrix portal exists but is unverified -> portal provenance/reconciliation problem;
3. verified portal has eligible unprocessed history but no Chatwoot link -> integration ingestion problem;
4. verified portal has only already-processed history -> persistent dedupe state explains why no replay occurred;
5. linked portal with Chatwoot conversation ID -> downstream Chatwoot state can be investigated directly.

Diagnostics are enabled by default. Set `INTEGRATION_META_DEBUG=false` to disable them or `INTEGRATION_META_DEBUG_INTERVAL` to change the interval; values below 30 seconds are clamped to 30 seconds.

## Clean-state acceptance

For a destructive acceptance test, clear the test environment's persistent Synapse, mautrix-meta, integration and Chatwoot state together. Do not delete only Chatwoot conversations while retaining integration dedupe state and then interpret missing replay as thread-discovery failure.

After connecting Facebook, capture both `mautrix-meta` and `integration` logs. The required evidence is:

- Meta socket reaches connected state;
- thread backfill starts and reports how many pages it processed;
- expected Matrix portals appear;
- integration `META_DEBUG inventory_summary` reports how many verified portals exist;
- every missing Chatwoot conversation can be classified by a `portal_state` reason rather than silently disappearing.
