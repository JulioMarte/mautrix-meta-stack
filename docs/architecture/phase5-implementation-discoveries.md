# Phase 5 Implementation Discoveries

Status: operational discovery record for `feature/chatwoot-matrix-phase-5`. Normative behavior is captured in `phase5-chatwoot-matrix.md` and the shared event contracts.

## Chatwoot webhook signatures are a first-class authentication boundary

Current Chatwoot supports per-webhook signing secrets and sends `X-Chatwoot-Timestamp` plus `X-Chatwoot-Signature`, where the signature is HMAC-SHA256 over `timestamp + "." + raw_request_body`.

Implication: the control plane must verify the signature against the unmodified raw body before trusting account, inbox, conversation, sender or message fields. JSON reserialization before verification would change the signed bytes and is invalid.

## Webhook signing secrets and Chatwoot API tokens are different credentials

The same Chatwoot integration needs an API token for outbound REST calls/attachment retrieval and a webhook secret for ingress authentication.

Implication: Phase 5 uses separate secret-reference namespaces. API credentials resolve only through `env:CHATWOOT_*` excluding the webhook prefix; webhook signatures resolve only through `env:CHATWOOT_WEBHOOK_*`. Persisting a generic reference that can resolve either role creates unnecessary credential-confusion risk.

## A binding-specific webhook URL avoids trusting an unsigned routing field

Selecting a webhook secret by first reading `account.id` or `inbox.id` from an unauthenticated body creates a circular trust problem.

Implication: the ingress path includes the opaque internal `chatwoot_binding` ID. That path component selects the candidate signing secret before the body is trusted; the signed body's account/inbox values are then checked against that binding.

## Chatwoot conversation API and webhook IDs align on display_id

Inspection of Chatwoot's conversation serializer and webhook presenter shows that the public `conversation.id` emitted at both boundaries is the conversation `display_id`.

Implication: the `chatwoot_conversation_id` persisted by Phase 4 can be used directly for Phase 5 webhook lookup. Do not later substitute Chatwoot's internal database primary key without an explicit migration/contract change.

## Chatwoot attachment data URLs can redirect to object storage

Chatwoot's attachment model documents that its file URL can issue a redirect to the actual stored object. A normal HTTP client that automatically follows redirects while retaining `api_access_token` could expose the Chatwoot API token to a storage origin.

Implication: Phase 5 performs explicit redirect handling. The initial URL must be same-origin with the configured Chatwoot base URL; one cross-origin redirect is allowed only to HTTPS and the Chatwoot token is stripped before following it.

## Matrix transaction IDs are the reconciliation primitive for ambiguous sends

A Matrix room-event send uses a client-provided `txnId`. Reusing the same transaction ID across retries lets a conforming homeserver return the same logical event rather than create a duplicate.

Implication: Phase 5 derives deterministic transaction IDs from the binding-scoped Chatwoot message identity and reuses them after retryable/ambiguous failures. The integration topology injects a post-commit Matrix failure and proves one stored event after retry.

## Bidirectional loop prevention requires explicit provenance at the Matrix boundary

Text equality, timestamps and sender names are insufficient to distinguish a Chatwoot reply reflected through mautrix from a genuine new Meta message.

Implication: every Chatwoot-originated Matrix event includes `com.mautrix_meta_stack.provenance` with `source=chatwoot` and the binding-scoped source event ID. Phase 4 treats that provenance as an echo and performs no Chatwoot side effect.
