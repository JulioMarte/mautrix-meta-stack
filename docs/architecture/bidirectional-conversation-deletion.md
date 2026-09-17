# Bidirectional conversation deletion

## Product contract

A linked Meta/Chatwoot conversation behaves as one lifecycle object:

- deleting a Messenger/Marketplace thread for the connected Meta account removes the linked Chatwoot conversation;
- deleting the linked Chatwoot conversation requests deletion of that same thread from the connected Meta account;
- this **does not** mean "delete for everyone". The Matrix event always uses `delete_for_everyone: false` and mautrix-meta v0.2608.1 uses its normal `DeleteThreadTask` path;
- a future thread/new activity may create a new Chatwoot conversation naturally. Deletion records are audit/idempotency records, not a permanent blocklist.

## Meta -> Chatwoot

mautrix-meta v0.2608.1 translates Meta thread deletion procedures (`LSDeleteThread`, `LSDeletePartialThread`, deleted message requests and self-leave) to BridgeV2 `RemoteEventChatDelete`. BridgeV2 owns deletion/unlinking of the Matrix portal.

The integration observes the resulting Matrix `/sync` `rooms.leave` entry. It deletes Chatwoot only when all of these are true:

1. the room still has a local `room_links` mapping;
2. the room was previously persisted in `verified_meta_portals` after authoritative Meta portal verification;
3. the leave event removes the dedicated integration Matrix account;
4. the sender of that membership leave is exactly the configured mautrix-meta bridge bot.

A manual Matrix leave, an unverified room, or an unmapped room is a no-op.

Before calling Chatwoot DELETE, the integration persists a `conversation_deletions` operation with `origin=meta`. This tombstone suppresses the Chatwoot deletion callback from being reflected back to Meta.

A temporary Chatwoot failure does **not** block Matrix `/sync` progress and does not lose the deletion. The operation moves to `failed_retryable`, persists a `next_retry_at` deadline, and is retried independently of Matrix with exponential backoff (30 seconds initially, capped at one hour). Reconciliation runs during later sync iterations and during lifecycle bootstrap after a process restart. A Chatwoot `404` during this retry is treated as idempotent success because the remote Meta deletion was already authoritatively confirmed before the retry record was created.

`remote_confirmed` itself is also recoverable. This matters if the integration process dies after persisting the authoritative Meta deletion but before it can call Chatwoot at all. On restart, reconciliation picks up that state immediately; it does not depend on Matrix redelivering the leave event.

After Chatwoot is confirmed gone, changing the operation to `completed` and removing `room_links` happen in one SQLite transaction. If the process dies after the external Chatwoot DELETE but before that transaction commits, the still-persisted `remote_confirmed`/`failed_retryable` operation is retried; the resulting Chatwoot `404` finalizes it idempotently.

## Chatwoot -> Meta

Stock Chatwoot internally dispatches `conversation.deleted`, but its stock `WebhookListener` does not forward that event to API inbox callbacks. In Chatwoot v4.7.0, the stock API-inbox delivery path is also not signed with the raw-body/timestamp HMAC contract required by this integration for destructive callbacks.

`chatwoot-extension/config/initializers/meta_conversation_delete_webhook.rb` adds exactly the missing `conversation_deleted` listener and queues `MetaConversationDeleteWebhookJob`. The job posts to the configured API inbox `webhook_url` and signs `timestamp + "." + raw_body` with that API channel's real `hmac_token`, producing `X-Chatwoot-Timestamp` and `X-Chatwoot-Signature` headers. The integration imports the same `hmac_token` from Chatwoot's authenticated inbox API and verifies that signature before allowing deletion to continue.

The integration accepts the event only on the signed API inbox callback. It then requires:

1. configured Chatwoot account ID match;
2. configured inbox ID match;
3. an existing `room_links` row for the deleted conversation ID;
4. a previously verified Meta portal registry row.

Only then does it send this Matrix event to the portal room:

```json
{
  "type": "com.beeper.delete_chat",
  "content": {
    "delete_for_everyone": false,
    "from_message_request": false
  }
}
```

BridgeV2 v0.30.0 routes `com.beeper.delete_chat` to mautrix-meta's `HandleMatrixDeleteChat`, which sends Meta's `DeleteThreadTask`.

The `room_links` row is deliberately retained after Matrix accepts the event. It is deleted only when BridgeV2 subsequently removes the portal and the trusted Matrix leave is observed. This keeps enough state for audit/recovery if the remote delete fails.

The Matrix transaction ID is deterministic for the conversation/room pair. If delivery of the signed Chatwoot callback is retried after an HTTP submission failure, resubmitting the same Matrix transaction remains idempotent at the Matrix client API boundary. Once Matrix has returned an event ID, the lifecycle records `remote_requested` and does not proactively emit additional destructive events; final confirmation comes from the bridge-owned portal deletion.

A trusted bridge confirmation may race the HTTP submit path and arrive while the operation is still `pending`. That direct `pending -> completed` transition is allowed because the bridge-owned portal deletion is stronger evidence than the submit response. The submit path re-reads state before writing `remote_requested`, so a fast confirmation cannot be overwritten by a stale state update.

## Persistent state

`verified_meta_portals` stores only room IDs and verification timestamps. It exists so a room that is already being deleted does not need to remain queryable in Synapse before its prior Meta provenance can be proven.

`conversation_deletions` stores:

- Chatwoot conversation ID;
- Matrix room ID;
- origin (`meta` or `chatwoot`);
- state;
- Matrix delete event ID when applicable;
- attempts and last error;
- `next_retry_at` for durable Meta -> Chatwoot reconciliation;
- created/updated timestamps.

Expected states are `pending`, `remote_requested`, `remote_confirmed`, `completed`, and `failed_retryable`. Runtime updates enforce allowed state transitions rather than treating the state string as unconstrained metadata.

The intended transitions are:

```text
Chatwoot origin:
pending -> remote_requested -> completed
pending -> completed            # bridge confirmation wins a fast race
pending -> failed_retryable -> remote_requested -> completed
failed_retryable -> completed   # bridge confirmation can arrive after an ambiguous HTTP failure

Meta origin:
remote_confirmed -> completed
remote_confirmed -> failed_retryable -> completed
failed_retryable -> failed_retryable   # another bounded retry failed
```

## Chatwoot deployment requirement

This repository does not own the user's Chatwoot deployment. The extension must be loaded by every Chatwoot process that can execute conversation deletion and webhook delivery, especially the Sidekiq worker where `DeleteObjectJob` destroys the conversation.

Build `chatwoot-extension/Dockerfile` with the **exact Chatwoot image/tag currently deployed**:

```sh
docker build \
  --build-arg CHATWOOT_BASE_IMAGE=chatwoot/chatwoot:<PINNED_VERSION> \
  -t chatwoot-meta-lifecycle:<PINNED_VERSION> \
  chatwoot-extension
```

Use the resulting image for both Chatwoot web and worker services. Do not use an unpinned `latest` tag for this extension.

After deployment, keep the API inbox callback URL unchanged. The signing key is the API channel's `hmac_token`; the integration's admin callback verification imports that token through the authenticated Chatwoot inbox API. No second unauthenticated webhook endpoint is introduced.

## Validation contract

CI runs the lifecycle unit/callback tests, boots the extension against pinned `chatwoot/chatwoot:v4.7.0` with PostgreSQL/pgvector and Redis, verifies that the real `Channel::Api` schema exposes both `webhook_url` and `hmac_token`, and confirms that the initializer queues a callback signed with that real token. The main `Validate stack` workflow also runs the bidirectional Docker journey with real Synapse, the real integration runtime, and the pinned mautrix-meta runtime.

The remaining non-automated boundary is Meta itself: CI does not log into a real Facebook/Instagram account. Production/staging acceptance should therefore still include a disposable Meta account canary for one destructive test in each direction.

## Safety decisions

- No polling of Facebook is added for deletion detection.
- A Chatwoot 404 discovered by normal message processing is not interpreted as proof that the user intended a Meta deletion.
- A Chatwoot 404 while reconciling an already-authoritatively-confirmed `origin=meta` deletion is idempotent success.
- The old "recreate deleted Chatwoot conversation" behavior is forcibly disabled by the lifecycle layer.
- Chatwoot -> Meta does not automatically emit another destructive Matrix event after Matrix has already returned an event ID; confirmation comes from the bridge-owned portal deletion.
- Meta -> Chatwoot failures are retried from durable local state because Meta deletion has already been confirmed and retrying the local Chatwoot DELETE cannot create a second remote Meta deletion.
- Retry reconciliation is serialized inside the integration process so bootstrap and regular Matrix sync cannot concurrently perform the same local cleanup.
- Deletion is scoped to the current account/inbox and preverified portal mapping.
