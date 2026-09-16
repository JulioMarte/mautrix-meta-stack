# Meta conversation discovery and Chatwoot materialization

Status: normative for `dev`

## Problem

A Facebook thread can exist as a valid mautrix-meta Matrix portal without being visible in Chatwoot. Historically the integration created the Chatwoot contact/conversation lazily while processing a qualifying inbound `m.room.message`. A portal whose recent history was already processed, outbound-only, media/state-heavy, or otherwise not accepted by that message path could therefore remain invisible.

A separate failure mode exists before Matrix: mautrix-meta loads an initial Facebook thread table and then normally starts paginated thread discovery from the Messagix `Ready` event. The managed stack configures `network.thread_backfill.batch_count: -1`, so discovery is intended to continue until Meta reports no additional page. A successful socket connection that processes the initial table without delivering `Ready` can strand discovery on that first table.

## Required behavior

1. A joined room is considered for materialization only after the existing authoritative Meta-portal provenance verification succeeds.
2. Matrix spaces, including the Marketplace grouping space, are never materialized as Chatwoot conversations.
3. A verified chat portal is allowed to create its Chatwoot room link before a new inbound message arrives, once a trustworthy remote Meta ghost can be identified.
4. Message ingestion remains responsible for copying actual message content; materialization must not fabricate messages.
5. The normal upstream `Ready -> StartThreadBackfill` path remains primary.
6. After a successful Meta socket connect, a delayed watchdog invokes the same `StartThreadBackfill` function only when initial thread backfill has not completed.
7. Normal and watchdog starts are serialized per login. Upstream `BackfillCompleted` semantics remain unchanged, so this is not recurring scraping.
8. The runtime bridge for this stack is built from the exact pinned upstream source plus only this narrow thread-discovery watchdog. The broader multitenant/provisioning fork remains a separate artifact and is not implicitly activated by this fix.

## Operational evidence

Useful runtime log markers are:

- `materialized Meta portal in Chatwoot` — a verified Matrix portal obtained a Chatwoot room link without waiting for a new message.
- `Starting thread backfill` — normal upstream pagination began from `Ready`.
- `Starting thread backfill from connect watchdog` — `Ready` did not start pagination before the fallback window.
- `Thread backfill complete` — pagination reached its configured/remote end and persisted completion.
- `verified Meta portal has no unambiguous remote participant yet` — the room is trusted but the integration intentionally refused to guess the contact identity.

## Safety boundary

The reconciler must prefer omission over linking a portal to the wrong contact. It accepts only appservice-owned Meta ghost identities, excludes the Matrix admin and bridge bot, and uses state as a fallback only when one remote ghost is unambiguous.
