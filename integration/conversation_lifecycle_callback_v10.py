"""Wire conversation deletion into the production signed Chatwoot callback path."""
from __future__ import annotations

import conversation_lifecycle_v10 as lifecycle
import delivery_history_v2 as delivery

_base_callback_handler = delivery.callback_outgoing_handler


def callback_handler(payload: dict, *, signature_verified: bool) -> dict:
    if payload.get("event") == "conversation_deleted":
        # Normal outgoing messages have a REST re-authentication fallback for old
        # Chatwoot installs with mismatched exposed/signing secrets. A deleted
        # conversation cannot be fetched after the fact, so destructive deletion
        # must require the API-inbox HMAC itself to verify.
        if not signature_verified:
            raise RuntimeError("conversation_deleted requires a verified Chatwoot API-inbox signature")
        return lifecycle.process_chatwoot_delete(payload)
    return _base_callback_handler(payload, signature_verified=signature_verified)


def install() -> None:
    delivery.callback_outgoing_handler = callback_handler
