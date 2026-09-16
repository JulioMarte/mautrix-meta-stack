# Meta thread discovery runtime and debugging

Status: operational contract for `dev`.

## Root causes and upstream limitations found on 2026-09-16

The stack configured mautrix-meta with `network.thread_backfill.batch_count: -1` and enabled message backfill, but the Compose runtime still launched the unmodified upstream `dock.mau.dev/mautrix/meta:v26.08.1` image by default. The repository's process-level thread-refresh patch lived only under `mautrix-meta-fork/` and was built by a dedicated CI job; normal `compose.yaml` did not build or run that patched binary.

This created a false acceptance signal: CI proved that a patched fork compiled, not that the deployed stack executed it.

Upstream v0.2608.1 persists `UserLoginMetadata.BackfillCompleted`. On later process starts, `StartThreadBackfill` returns immediately when that marker is true, before calling `runThreadBackfill`. Therefore `batch_count: -1` means unlimited pages only when thread backfill actually runs; it does not override the persisted completion guard.

A second upstream limitation is in thread pagination. Meta's own initial fetch code has thread key stores for sync groups `1` and `95`, and the initial fetch requests both groups. Upstream `runThreadBackfill`, however, paginates only `FetchMoreThreads(ctx, 1)` and even carries the source comment `TODO: other SyncGroups?`. That means a successful completion marker can be persisted after exhausting group 1 without ever paging older threads that remain in group 95.

A third upstream failure mode affects message history after a portal exists. `FetchMessages` requires a Facebook message ID when it has to derive a history anchor from the oldest bridged message. If `metaid.ParseMessageID` does not return `ParsedFBMessageID`, upstream logs `Can't backfill with non-FB message ID` and returns no history. Upstream issue #327 documents this symptom on the v26.08 development line. The runtime does not guess a replacement anchor because sending an invalid or fabricated message ID to Meta could corrupt pagination semantics.

## Runtime contract

The normal `mautrix-meta` Compose service builds `mautrix-meta-runtime/Dockerfile` from the exact upstream v0.2608.1 commit `001f276beca5b90dead1bbc1351e1036e3f966a7`.

The runtime delta is intentionally bounded and guarded against upstream drift:

- add one process-local atomic guard to `MetaClient`;
- allow a persisted-complete thread backfill to run once after each process start;
- prevent duplicate concurrent triggers;
- release the process-local guard on failure so a reconnect can retry;
- paginate both known Facebook thread sync groups, `1` and `95`, before persisting `BackfillCompleted`;
- preserve the configured global positive `batch_count` semantics when a finite limit is used;
- emit safe thread-discovery and message-history diagnostics without logging thread IDs, message IDs, message bodies, cookies, tokens, or raw Meta payloads.

The configuration/registration one-shot containers remain on the official v26.08.1 image. Runtime and generated configuration therefore target the same upstream release instead of mixing an old fork baseline with a newer generated config.

A candidate is not acceptable merely because `mautrix-meta-runtime/Dockerfile` builds. CI must also assert that the `mautrix-meta` service in `compose.yaml` references that Dockerfile and that the source-transform guard still matches the pinned upstream commit exactly.

## Why sync group 95 is now paginated

This is no longer a speculative Marketplace-specific mapping. The safe invariant comes directly from upstream architecture: the Messagix sync manager maintains thread key stores for groups 1 and 95, and the initial thread fetch requests both. Therefore a full thread-pagination pass must not mark itself complete after exhausting only one of those two known stores.

The patch still does not invent support for arbitrary sync groups. It exhausts exactly the two groups upstream already treats as thread stores.

## Expected mautrix-meta thread logs

On a fresh login, the bridge should log `Starting thread backfill`.

When a persisted login reconnects after a bridge/container restart, it should log `Re-running thread backfill once after process start despite persisted completion marker`.

For each known thread store it should then emit `Starting thread backfill sync group` with `sync_group=1` and later `sync_group=95`. Completion is persisted only after both groups finish, unless a configured positive global batch limit is hit.

The safe discovery telemetry line is:

`META_THREAD_DIAG fetch_more_threads_response`

It reports only aggregate routing information such as:

- `sync_group_requested`;
- inserted/updated/verified thread counts;
- returned sync-group counts;
- returned folder buckets;
- count of distinct parent-thread keys;
- sync-group/thread-range/folder update counts.

It intentionally does not emit raw thread keys, names, snippets, cookies, tokens, or raw response payloads.

## Expected mautrix-meta message-history logs

The runtime also emits `META_HISTORY_DIAG` at the points that distinguish the known history failure modes:

- `forward_backfill_without_bundled_data`: BridgeV2 requested a forward fill in a state where upstream refuses to fetch without bundled data;
- `non_fb_anchor`: the current anchor cannot be parsed as a Facebook message ID, matching the upstream #327 failure class;
- `no_anchor_or_bundled_messages`: there is neither an anchor nor bundled Meta history to start from;
- `collector_timeout`: Meta history collection did not complete before the upstream timeout;
- `fetch_messages_complete`: a history request completed and reports requested count, returned count and `has_more` only.

The existing upstream `Received upsert messages` log remains useful because it reports `message_count`, `has_more_before`, and `has_more_after` for Meta history batches.

These diagnostics deliberately observe the non-FB-anchor bug rather than attempting an unverified fallback. A functional fallback should only be added once live evidence proves which alternative cursor/reference tuple Meta accepts for that failure case.

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

1. Facebook UI has a thread but no `META_THREAD_DIAG`/Matrix portal -> Meta discovery or thread-pagination layer;
2. thread telemetry shows the conversation population but no Matrix portal -> mautrix table-to-portal/event processing;
3. Matrix portal exists but Meta history emits `non_fb_anchor`, timeout, or zero returned messages -> mautrix message-backfill layer;
4. Matrix portal contains historical `m.room.message` events but Chatwoot does not -> integration ingestion/dedupe layer;
5. linked portal with Chatwoot conversation ID -> downstream Chatwoot state can be investigated directly.

Diagnostics are enabled by default. Set `INTEGRATION_META_DEBUG=false` to disable them or `INTEGRATION_META_DEBUG_INTERVAL` to change the interval; values below 30 seconds are clamped to 30 seconds.

## Historical direction caveat

The current Chatwoot integration is intentionally conservative about messages sent by the dedicated Matrix admin/bridge identity in order to prevent loops. Therefore complete bidirectional historical reconstruction in Chatwoot is not yet claimed. Before importing historical self-sent Facebook messages as Chatwoot `outgoing`, the runtime must have a reliable event-origin marker that distinguishes remote historical self-messages from locally-originated Chatwoot/Matrix agent messages. Do not remove that sender guard merely to make history look complete; doing so can echo agent messages back into Chatwoot or Meta.

For acceptance, the first control message should be an older inbound customer message. If that inbound message is absent from Matrix, the fault is before Chatwoot. If it exists in Matrix but is absent from Chatwoot, the fault is in the integration layer.

## Clean-state acceptance

For a destructive acceptance test, clear the test environment's persistent Synapse, mautrix-meta, integration and Chatwoot state together. Do not delete only Chatwoot conversations while retaining integration dedupe state and then interpret missing replay as thread-discovery failure.

After connecting Facebook, capture both `mautrix-meta` and `integration` logs. The required evidence is:

- Meta socket reaches connected state;
- thread backfill starts;
- both sync groups 1 and 95 are attempted;
- `META_THREAD_DIAG` reports thread counts for each page;
- expected Matrix portals appear;
- message-history attempts can be classified with `META_HISTORY_DIAG` and upstream upsert counts;
- integration `META_DEBUG inventory_summary` reports how many verified portals exist;
- every missing Chatwoot conversation or historical inbound message can be assigned to a specific pipeline boundary rather than silently disappearing.
