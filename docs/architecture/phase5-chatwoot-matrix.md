# Phase 5 — Chatwoot to Matrix

Status: implemented contract for `feature/chatwoot-matrix-phase-5`; Phase 5 is complete only when the exact candidate SHA and its PR-to-`dev` gates are green.

## Scope

Phase 5 implements the return direction:

```text
Chatwoot human agent reply
  -> signed Chatwoot webhook
  -> Meta Control Plane
  -> exact conversation_binding
  -> Matrix Client API
  -> mautrix-meta
  -> bound Meta thread
```

The control plane does not infer a tenant or Matrix room from message text, sender identity, or a global Chatwoot conversation number.

## Webhook authentication

Each `chatwoot_binding` has an optional webhook configuration stored separately from its API credential. The public webhook path is binding-specific:

```text
POST /webhooks/chatwoot/<binding-id>
```

The binding ID selects the signing configuration before the request body is trusted.

The supported Chatwoot signature contract is:

```text
X-Chatwoot-Timestamp: <unix timestamp>
X-Chatwoot-Signature: sha256=<HMAC-SHA256(secret, timestamp + "." + raw_body)>
```

Signatures are verified against the raw request bytes with a bounded timestamp tolerance and constant-time digest comparison. Missing, stale, malformed or invalid signatures fail closed.

Webhook signing secrets MUST use `env:CHATWOOT_WEBHOOK_*`. Chatwoot API credentials use the separate `env:CHATWOOT_*` namespace; the two providers reject each other's references. Raw values are never persisted or rendered.

## Accepted message class

Only Chatwoot `message_created` events satisfying all of the following are eligible for Matrix delivery:

- `message_type == "outgoing"`;
- `private != true`;
- `sender.type == "user"`;
- positive account, inbox, conversation, message and sender identifiers;
- structurally valid attachments when present.

Incoming messages, private notes, bot/system senders and unsupported webhook event types are ignored without a Matrix side effect.

## Routing

The signed payload's account, inbox and conversation identifiers must match the selected `chatwoot_binding` and one persisted `conversation_binding`:

```text
tenant_id
chatwoot_account_id
chatwoot_inbox_id
chatwoot_conversation_id
 -> meta_connection_id
 -> matrix_room_id
```

The corresponding tenant, Meta connection and Chatwoot binding must all remain active. Missing or conflicting routing fails closed; no cross-tenant fallback or guessing is allowed.

Chatwoot's API and webhook conversation `id` are the conversation `display_id`, so Phase 4's persisted `chatwoot_conversation_id` is the identifier consumed by Phase 5.

## Matrix delivery and idempotency

Chatwoot events are claimed in `processed_events` under source `chatwoot`. The source event identity is scoped by binding and remote message ID.

Matrix room messages use deterministic transaction IDs derived from that source identity. Text is sent as `m.text`; binary attachments are uploaded using the Matrix media upload API and then sent as `m.image`, `m.audio`, `m.video` or `m.file`.

Every outbound Matrix message includes explicit provenance:

```json
{
  "com.mautrix_meta_stack.provenance": {
    "source": "chatwoot",
    "source_event_id": "<binding-id>:<chatwoot-message-id>"
  }
}
```

Retries after an ambiguous Matrix response reuse the same Matrix transaction ID. A homeserver-compatible receiver can therefore return the original event instead of creating another room event.

Phase 4 must recognize Chatwoot provenance and suppress the reflected Matrix event from being exported back to Chatwoot.

## Attachments

Webhook attachments use Chatwoot's `data_url`, `file_type`, MIME type, size and extension metadata. The control plane downloads bytes before Matrix upload.

The initial attachment URL MUST be same-origin with the configured Chatwoot `api_base_url`. Requests to that origin carry the Chatwoot API token. One explicit HTTPS cross-origin redirect is allowed for Active Storage/object-storage delivery, but the Chatwoot token MUST be stripped before following it.

Downloads and Matrix operations use bounded timeouts and configurable size limits. Attachment URLs, tokens and media bytes must not be logged.

## Required Phase 5 CI proof

The dedicated Phase 5 topology MUST prove all of the following on the exact candidate SHA:

1. a conversation binding created through the Phase 4 path is reused by Phase 5;
2. a correctly signed human outgoing webhook reaches the exact Matrix room;
3. text, representative audio and PDF/file bytes reach the Matrix-compatible boundary;
4. Matrix events contain explicit Chatwoot provenance;
5. duplicate webhook delivery causes no second Matrix side effect;
6. invalid signatures cause no Matrix side effect;
7. private messages and route mismatches cause no Matrix side effect;
8. an ambiguous post-commit Matrix failure is retried with the same transaction ID and produces one logical Matrix event;
9. control-plane restart preserves deduplication/routing state;
10. a reflected Chatwoot-provenance Matrix event is suppressed by Phase 4;
11. API token, webhook secret, Matrix token and control-plane bearer tokens do not appear in topology logs.

Passing unit tests alone is not Phase 5 completion evidence.
