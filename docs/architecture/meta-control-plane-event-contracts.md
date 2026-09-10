# Meta Control Plane Messaging Event Contracts

Status: normative for `feature/meta-control-plane`

## Purpose

Matrix and Chatwoot payloads MUST be normalized at adapter boundaries so provider-specific schemas do not leak through the domain/application layer.

## Canonical message

```ts
type Attachment = {
  id?: string
  kind: "image" | "video" | "audio" | "file" | "unknown"
  url?: string
  mimeType?: string
  fileName?: string
  sizeBytes?: number
}

type NormalizedMessage = {
  tenantId: string
  connectionId: string
  conversationExternalId: string
  messageExternalId: string
  senderExternalId: string
  senderDisplayName?: string
  direction: "inbound" | "outbound"
  text?: string
  attachments: Attachment[]
  occurredAt: string
  source: "matrix" | "chatwoot"
  sourceEventId: string
}
```

`messageExternalId` and `sourceEventId` MUST preserve provider identifiers needed for deduplication and correlation. Internal database IDs MUST not replace remote identities.

## Matrix -> Chatwoot

The Matrix adapter consumes only events from rooms attributable to a known active Meta connection. It MUST resolve the connection/tenant before any Chatwoot side effect.

Events created by our own outbound Chatwoot-to-Matrix path MUST be recognizable and suppressed from re-export to Chatwoot.

Unsupported Matrix event types MUST be ignored or explicitly dead-lettered; they MUST NOT be coerced into misleading text messages.

For a new remote thread the adapter creates or resolves a Chatwoot contact/source/conversation, persists the `conversation_binding`, then sends the message. Existing bindings MUST be reused.

## Chatwoot -> Matrix

The webhook adapter MUST authenticate/validate the incoming request using the strongest mechanism supported by the configured Chatwoot deployment and network topology. Payloads MUST be validated before routing.

Only agent/outgoing messages intended for delivery to Meta are forwarded. Incoming messages that originated from Matrix/Meta MUST not be reflected back.

The adapter resolves the `conversation_binding` and sends to the exact bound Matrix room. Absence or ambiguity of a binding is a hard routing failure; cross-tenant guessing is forbidden.

## Idempotency

Before an externally visible side effect, the service SHOULD atomically claim `(source, source_event_id)` in `processed_events`. Duplicate claims MUST return the prior processing state rather than repeat delivery.

Retries after ambiguous network failure MUST either use a downstream idempotency/correlation key where supported or reconcile the remote result before resending.

The system MUST distinguish `received`, `processing`, `delivered`, `failed_retryable`, and `failed_terminal` or equivalent states so crash recovery is deterministic.

## Ordering

Global ordering is not required. Per-conversation processing SHOULD preserve provider event order when practical. A late event MUST not be routed to a different conversation because a newer event arrived first.

## Attachments

Attachment support MUST be explicit by type. The first release may support a subset, but unsupported media MUST result in a visible/observable non-delivery state rather than silent data loss. Media downloads and uploads must respect tenant egress/security policy where applicable and must not expose private URLs in logs.

## Conversation identity

A conversation binding MUST anchor at minimum:

```text
tenant_id
meta_connection_id
matrix_room_id
remote_thread_id
chatwoot_account_id
chatwoot_inbox_id
chatwoot_conversation_id
```

A binding from tenant A MUST never satisfy a lookup for tenant B even if remote IDs collide.

## Loop prevention

Loop prevention MUST use explicit provenance/correlation data, not heuristics based only on message text, sender display name or timestamps.

## Failure handling

Chatwoot unavailable -> retain retryable state; do not acknowledge permanent delivery internally.
Matrix unavailable -> retain retryable state.
Binding missing/ambiguous -> terminal or operator-action-required state, never guess.
Tenant/connection disabled -> do not deliver.
Duplicate event -> no new downstream side effect.

## Required CI proofs

CI MUST prove inbound and outbound normalization, two-tenant routing separation, duplicate webhook/event suppression, restart recovery, missing-binding failure, echo-loop prevention, retry behavior and at least text plus one representative attachment path before claiming bidirectional messaging complete.