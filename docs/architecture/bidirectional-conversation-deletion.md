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

## Chatwoot -> Meta

Stock Chatwoot internally dispatches `conversation.deleted`, but its stock `WebhookListener` does not forward that event to API inbox callbacks. `chatwoot-extension/config/initializers/meta_conversation_delete_webhook.rb` adds exactly that missing forwarding method and reuses Chatwoot's existing `deliver_api_inbox_webhooks` path, preserving the channel secret signature, webhook job retry behavior and delivery ID.

The integration accepts the event only on the already-signed API inbox callback. It then requires:

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

## Persistent state

`verified_meta_portals` stores only room IDs and verification timestamps. It exists so a room that is already being deleted does not need to remain queryable in Synapse before its prior Meta provenance can be proven.

`conversation_deletions` stores:

- Chatwoot conversation ID;
- Matrix room ID;
- origin (`meta` or `chatwoot`);
- state;
- Matrix delete event ID when applicable;
- attempts/error timestamps.

Expected states are `pending`, `remote_requested`, `remote_confirmed`, `completed`, and `failed_retryable`.

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

After deployment, the existing API inbox callback URL and secret remain unchanged. No second unauthenticated webhook endpoint is introduced.

## Safety decisions

- No polling of Facebook is added for deletion detection.
- A Chatwoot 404 is not interpreted as proof that the user intended a Meta deletion.
- The old "recreate deleted Chatwoot conversation" behavior is forcibly disabled by the lifecycle layer.
- No automatic retry is issued after Matrix already accepted a destructive delete event. Confirmation comes from the bridge-owned portal deletion. Failed HTTP submission remains `failed_retryable` for the signed webhook/job retry path.
- Deletion is scoped to the current account/inbox and preverified portal mapping.
