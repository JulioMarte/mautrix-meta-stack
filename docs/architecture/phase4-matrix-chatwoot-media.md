# Phase 4 Matrix -> Chatwoot media and attachment contract

Status: implemented on `feature/matrix-chatwoot-phase-4`; this document records the operational discoveries and acceptance boundary for Phase 4 media delivery.

## Chatwoot attachment transport

Chatwoot accepts attachments on the normal conversation message-create endpoint as `multipart/form-data`. Files are sent as repeated `attachments[]` fields; text message metadata remains on the same request. The current Chatwoot model permits at most 15 attachments per message, so the adapter rejects a larger set before starting downstream side effects.

The gateway preserves `mautrix_meta_source_event_id` in `content_attributes` for both JSON-only and multipart messages. Before a create it searches for the same source event; after an ambiguous create failure it searches again before allowing a retry. This makes attachment delivery subject to the same idempotency rule as text delivery rather than treating binary uploads as fire-and-forget.

Chatwoot distinguishes an ordinary audio attachment from a voice message with `is_voice_message`. The normalized attachment contract therefore carries an explicit `voiceNote` boolean. It is accepted only for `kind: audio`; when true the multipart request sets `is_voice_message=true`. Filename or MIME heuristics must not invent voice-note semantics.

## Matrix media retrieval

An `mxc://` URI is an identifier, not a downloadable HTTP URL for Chatwoot. The control plane downloads the bytes from the configured homeserver through the authenticated Matrix client-media download endpoint and then uploads those bytes to Chatwoot.

The Matrix media access token is sent only in the `Authorization` header. The downloader accepts only `mxc://` attachment locations from ingress, applies a bounded timeout and maximum byte limit, and does not accept an arbitrary HTTP URL supplied by an event. This prevents the attachment path from becoming a general-purpose SSRF primitive.

Redirects are handled explicitly. Same-origin redirects may retain Matrix authentication; a cross-origin redirect must be HTTPS and never receives the homeserver bearer token. Unsupported or malformed redirects fail closed.

## Encrypted Matrix attachments

Matrix encrypted-file metadata is preserved in the normalized attachment contract when present. Phase 4 supports the Matrix v2 encrypted-file shape used for media: AES-256-CTR key material, IV/counter and SHA-256 ciphertext hash.

Before decryption, the downloaded ciphertext SHA-256 is compared with the expected hash. A mismatch fails closed. Only verified ciphertext is decrypted and converted into the Blob sent to Chatwoot. This prevents an E2EE room attachment from being forwarded as unreadable ciphertext or silently accepting corrupted bytes.

The current implementation treats encryption metadata as event-supplied cryptographic material from the trusted Matrix-side adapter. It does not fetch arbitrary key material from external URLs.

## CI acceptance evidence

The dedicated Phase 4 topology uses a Matrix media double requiring bearer authentication plus a Chatwoot double that parses actual multipart requests. It proves image, OGG voice note and PDF transfer as binary attachments, preserves filename and MIME type, observes native voice-note semantics, keeps two tenant/inbox routes isolated, suppresses duplicate replay, reconciles an injected post-commit Chatwoot failure, survives a control-plane restart without duplicating the media message, and rejects synthetic credential leakage in logs.

Unit coverage additionally proves authenticated Matrix media URL construction, arbitrary HTTP attachment rejection, size limits, the 15-attachment Chatwoot limit, AES-CTR encrypted-media decryption and SHA-256 corruption detection.

A Phase 4 candidate is not considered complete until both the dedicated `Phase 4 Matrix to Chatwoot` workflow and the repository-wide `Validate stack` workflow are green on the exact same candidate SHA. After merge, `dev` must also be green before Phase 4 is called integrated.
