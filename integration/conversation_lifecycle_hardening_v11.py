"""Adversarial race hardening for bidirectional conversation deletion.

The v10 lifecycle persists an operation before remote effects. This layer closes
remaining concurrency windows by making the lifecycle lock authoritative for the
mapping lookup, operation claim and destructive side effect. Target migration uses
the same lock, so a configuration change cannot interleave with an old-target delete.
"""
from __future__ import annotations

import time

import conversation_lifecycle_v10 as lifecycle

legacy = lifecycle.legacy
_INSTALLED = False


def _chatwoot_origin_confirmation(conversation_id: int, room_id: str) -> dict:
    lifecycle._delete_room_link(room_id, conversation_id)
    current = lifecycle._operation(conversation_id)
    if current and current["state"] != "completed":
        lifecycle._update_operation(
            conversation_id,
            state="completed",
            error="",
            next_retry_at=0,
        )
    print(
        f"conversation lifecycle: Chatwoot-origin delete confirmed by Meta "
        f"room={room_id} conversation={conversation_id}",
        flush=True,
    )
    return {"ok": True, "completed": True, "origin": "chatwoot"}


def process_matrix_leave(room_id: str, room: dict) -> dict:
    """Resolve mapping, provenance, origin and HTTP effect under one lock."""
    with lifecycle._RECONCILE_LOCK:
        link = lifecycle._link_by_room(room_id)
        if not link:
            return {"ok": True, "ignored": True, "reason": "unmapped_room"}
        if not lifecycle.portal_was_verified(room_id):
            return {"ok": True, "ignored": True, "reason": "portal_not_preverified"}
        if not lifecycle._trusted_bridge_leave(room):
            print(f"conversation lifecycle: untrusted Matrix leave ignored room={room_id}", flush=True)
            return {"ok": True, "ignored": True, "reason": "leave_not_from_bridge_bot"}

        conversation_id = int(link["conversation_id"])
        existing = lifecycle._operation(conversation_id)
        if existing and existing["origin"] == "chatwoot":
            return _chatwoot_origin_confirmation(conversation_id, room_id)

        lifecycle._start_operation(conversation_id, room_id, "meta", "remote_confirmed")
        claimed = lifecycle._operation(conversation_id)
        if not claimed:
            raise RuntimeError(f"conversation lifecycle claim disappeared: {conversation_id}")

        # Chatwoot may have won INSERT OR IGNORE after an earlier callback entered.
        # The Meta leave then serves as that operation's remote confirmation.
        if claimed["origin"] == "chatwoot":
            return _chatwoot_origin_confirmation(conversation_id, room_id)
        if claimed["origin"] != "meta":
            raise RuntimeError(
                f"conversation lifecycle origin conflict conversation={conversation_id} "
                f"origin={claimed['origin']}"
            )

        state = str(claimed["state"])
        if state == "completed":
            lifecycle._delete_room_link(room_id, conversation_id)
            return {"ok": True, "completed": True, "origin": "meta", "duplicate": True}

        # A replayed /sync leave must not bypass a previously scheduled backoff.
        if state == "failed_retryable":
            retry_at = int(claimed["next_retry_at"] or 0)
            if retry_at > int(time.time()):
                return {
                    "ok": True,
                    "pending": True,
                    "origin": "meta",
                    "reason": "retry_scheduled",
                    "next_retry_at": retry_at,
                }

        try:
            lifecycle._complete_meta_delete(conversation_id, room_id)
        except Exception as exc:
            lifecycle._mark_retryable(conversation_id, exc)
            raise

    print(
        f"conversation lifecycle: Meta delete propagated to Chatwoot "
        f"room={room_id} conversation={conversation_id}",
        flush=True,
    )
    return {"ok": True, "completed": True, "origin": "meta"}


def process_chatwoot_delete(payload: dict) -> dict:
    """Claim origin and send BridgeV2 delete_chat under the lifecycle lock."""
    in_scope, reason = lifecycle._configured_scope(payload)
    if not in_scope:
        return {"ok": True, "ignored": True, "reason": reason}
    try:
        conversation_id = int(payload.get("conversation_id") or payload.get("id"))
    except (TypeError, ValueError):
        return {"ok": True, "ignored": True, "reason": "missing_conversation_id"}

    with lifecycle._RECONCILE_LOCK:
        existing = lifecycle._operation(conversation_id)
        if existing:
            if existing["origin"] == "meta":
                return {"ok": True, "ignored": True, "reason": "meta_delete_loop_suppressed"}
            if existing["state"] in {"remote_requested", "completed"}:
                return {"ok": True, "duplicate": True, "state": existing["state"]}

        link = lifecycle._link_by_conversation(conversation_id)
        if not link:
            return {"ok": True, "ignored": True, "reason": "unmapped_conversation"}
        room_id = str(link["room_id"])
        if not lifecycle.portal_was_verified(room_id):
            return {"ok": True, "ignored": True, "reason": "portal_not_preverified"}

        lifecycle._start_operation(conversation_id, room_id, "chatwoot", "pending")
        claimed = lifecycle._operation(conversation_id)
        if not claimed:
            raise RuntimeError(f"conversation lifecycle claim disappeared: {conversation_id}")

        # Meta may have won INSERT OR IGNORE after the callback's earlier scope
        # checks. Never emit a delete_chat event for the loopback in that case.
        if claimed["origin"] == "meta":
            return {"ok": True, "ignored": True, "reason": "meta_delete_loop_suppressed"}
        if claimed["origin"] != "chatwoot":
            raise RuntimeError(
                f"conversation lifecycle origin conflict conversation={conversation_id} "
                f"origin={claimed['origin']}"
            )
        if claimed["state"] in {"remote_requested", "completed"}:
            return {"ok": True, "duplicate": True, "state": claimed["state"]}

        try:
            event_id = lifecycle._send_matrix_delete(room_id, conversation_id)
        except Exception as exc:
            current = lifecycle._operation(conversation_id)
            if current and current["state"] == "completed":
                return {
                    "ok": True,
                    "completed": True,
                    "origin": "chatwoot",
                    "confirmed_during_submit": True,
                }
            lifecycle._update_operation(
                conversation_id,
                state="failed_retryable",
                error=str(exc),
                increment_attempts=True,
                next_retry_at=0,
            )
            raise

        current = lifecycle._operation(conversation_id)
        if current and current["state"] == "completed":
            return {
                "ok": True,
                "completed": True,
                "origin": "chatwoot",
                "confirmed_during_submit": True,
            }
        lifecycle._update_operation(
            conversation_id,
            state="remote_requested",
            matrix_event_id=event_id,
            error="",
            increment_attempts=True,
            next_retry_at=0,
        )

    print(
        f"conversation lifecycle: Chatwoot delete requested on Meta "
        f"room={room_id} conversation={conversation_id} event={event_id}",
        flush=True,
    )
    return {"ok": True, "remote_requested": True, "event_id": event_id}


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    lifecycle.process_matrix_leave = process_matrix_leave
    lifecycle.process_chatwoot_delete = process_chatwoot_delete
    _INSTALLED = True
