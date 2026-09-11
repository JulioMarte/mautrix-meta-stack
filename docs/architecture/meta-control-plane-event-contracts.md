# Meta Control Plane Messaging Event Contracts

Status: normative for `feature/meta-control-plane`

## Purpose

Matrix and Chatwoot payloads MUST be normalized at adapter boundaries so provider-specific schemas do not leak through the domain/application layer.

## Canonical message

```ts
type MatrixEncryptedFile = {
  v: "v2"
  key: {
    kty: "oct"
    alg: "A256CTR"
    k: string
    keyOps: string[]
    ext: true
  }
  iv: string
  hashes: { sha256: string }
}

type Attachment = {
  id?: string
  kind: "image" | "video" | "audio" | "file" | "unknown"
  url?: string
  mimeType?: string
  fileName?: string
  sizeBytes?: number
  voiceNote?: boolean
  encryption?: MatrixEncryptedFile
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

`voiceNote` is explicit semantic metadata, not a filename or MIME inference. It MAY be true only when `kind` is `audio`; when true, the Chatwoot adapter MUST request native voice-message semantics while still uploading the audio as a normal binary attachment.

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

Phase 4 Matrix -> Chatwoot supports binary image, video, audio/voice-note and generic file attachments, including PDFs, through Chatwoot's message attachment surface.

The adapter MUST NOT forward an `mxc://` URI as though it were a public Chatwoot attachment. It must retrieve the bytes from the configured Matrix homeserver using the authenticated Matrix client-media download endpoint, then submit the files to Chatwoot as `multipart/form-data` fields named `attachments[]` on the same conversation-message endpoint used for text.

Media retrieval MUST:

- accept only Matrix Content URIs (`mxc://`) from the normalized event; arbitrary event-provided `http://` or `https://` download URLs are forbidden;
- authenticate to the configured homeserver using a header-carried access token, never a query-string token;
- use a bounded timeout and configurable maximum media size;
- handle redirects explicitly and MUST NOT forward the Matrix bearer token to a different origin;
- preserve a safe filename and MIME type when known;
- fail visibly rather than silently dropping an attachment.

For Matrix encrypted attachments (`EncryptedFile` v2), the normalized adapter MUST preserve the `file` encryption metadata. Before uploading to Chatwoot the control plane MUST verify the SHA-256 hash of the ciphertext and decrypt it using the specified AES-256-CTR key/IV. Hash mismatch, malformed encryption metadata or decryption failure is a non-delivery state. Ciphertext MUST NOT be uploaded to Chatwoot as if it were the original media.

The current Chatwoot model permits at most 15 attachments on one message. The adapter therefore fails before the Chatwoot POST when more than 15 attachments are supplied, rather than relying on a provider-side partial failure.

Media bytes, Matrix access tokens, Chatwoot API tokens and private media URLs MUST NOT be logged.

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
Matrix media unavailable -> retryable unless the event/media metadata is structurally invalid.
Matrix encrypted-media hash mismatch -> terminal/operator-visible failure; never upload unverified bytes.

## Required CI proofs

CI MUST prove inbound and outbound normalization, two-tenant routing separation, duplicate webhook/event suppression, restart recovery, missing-binding failure, echo-loop prevention, retry behavior and at least text plus representative image, audio/voice-note and file/PDF attachment paths before claiming bidirectional messaging complete.

For Matrix -> Chatwoot attachment support specifically, CI MUST observe a real multipart HTTP request at a Chatwoot-compatible boundary, authenticated Matrix media retrieval, exact tenant/inbox routing, duplicate suppression, retry reconciliation after an ambiguous Chatwoot response, restart persistence and encrypted-media hash/decryption behavior.
