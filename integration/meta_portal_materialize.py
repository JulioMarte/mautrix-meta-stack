"""Materialize verified Meta chat portals in Chatwoot before message ingestion.

The message pipeline historically created a Chatwoot conversation lazily when the
first qualifying inbound text event was processed. That leaves valid Facebook
threads invisible when their recent Matrix history contains only already-seen,
outbound, media, or state events. This module reconciles the portal itself as the
source of truth and creates the room link as soon as we can identify a remote Meta
participant.
"""
from __future__ import annotations

import threading
from urllib.parse import quote

import autojoin_verify
import final_app as runtime
import meta_portal_reconcile as reconcile
import runtime_enhancements as enhancements

legacy = runtime.legacy

MATERIALIZE_INTERVAL_SECONDS = 30
_HISTORY_LIMIT = 200
_STOP = threading.Event()
_LOCK = threading.Lock()


def _is_space(state: list[dict]) -> bool:
    for event in state:
        if not isinstance(event, dict):
            continue
        content = event.get("content") or {}
        if not isinstance(content, dict):
            continue
        if event.get("type") == "m.room.create" and content.get("type") == "m.space":
            return True
        if content.get("com.beeper.room_type.v2") == "space":
            return True
    return False


def _trusted_remote_sender(sender: str) -> bool:
    if not sender or sender == legacy.MATRIX_ADMIN_MXID:
        return False
    trust = autojoin_verify.appservice_trust()
    if sender == trust.bot_mxid:
        return False
    trusted, _reason = autojoin_verify.trusted_meta_inviter(sender)
    return trusted


def _sender_from_history(room_id: str) -> str:
    encoded = quote(room_id, safe="")
    response = enhancements._matrix_get(
        f"/_matrix/client/v3/rooms/{encoded}/messages?dir=b&limit={_HISTORY_LIMIT}",
        timeout=30,
    )
    data = response.json() if response.content else {}
    chunk = data.get("chunk") if isinstance(data, dict) else []
    if not isinstance(chunk, list):
        return ""
    for event in chunk:
        if not isinstance(event, dict):
            continue
        if event.get("type") != "m.room.message":
            continue
        sender = str(event.get("sender") or "")
        if _trusted_remote_sender(sender):
            return sender
    return ""


def _sender_from_state(state: list[dict]) -> str:
    """Use state only when it identifies exactly one remote ghost unambiguously."""
    candidates: list[str] = []
    for event in state:
        if not isinstance(event, dict) or event.get("type") != "m.room.member":
            continue
        content = event.get("content") or {}
        if not isinstance(content, dict) or content.get("membership") not in {"join", "invite"}:
            continue
        mxid = str(event.get("state_key") or "")
        if _trusted_remote_sender(mxid):
            candidates.append(mxid)
    unique = list(dict.fromkeys(candidates))
    return unique[0] if len(unique) == 1 else ""


def materialize_meta_portals() -> dict[str, int]:
    """Ensure every verified joined Meta chat portal has a Chatwoot room link."""
    if not _LOCK.acquire(blocking=False):
        return {"joined": 0, "verified": 0, "materialized": 0, "skipped_space": 0, "unresolved": 0}

    result = {"joined": 0, "verified": 0, "materialized": 0, "skipped_space": 0, "unresolved": 0}
    try:
        memberships = reconcile.user_memberships()
        for room_id, membership in memberships.items():
            if membership != "join":
                continue
            result["joined"] += 1
            if reconcile._link_exists(room_id):
                continue

            verified, _reason = reconcile.verified_meta_portal(room_id)
            if not verified:
                continue
            result["verified"] += 1

            state = reconcile.room_admin_state(room_id)
            if _is_space(state):
                result["skipped_space"] += 1
                continue

            sender = _sender_from_history(room_id) or _sender_from_state(state)
            if not sender:
                result["unresolved"] += 1
                print(
                    f"verified Meta portal has no unambiguous remote participant yet room={room_id}",
                    flush=True,
                )
                continue

            enhancements.enhanced_ensure_room_link(room_id, sender)
            if reconcile._link_exists(room_id):
                result["materialized"] += 1
                print(
                    f"materialized Meta portal in Chatwoot room={room_id} remote_sender={sender}",
                    flush=True,
                )
        return result
    finally:
        _LOCK.release()


def _loop() -> None:
    while not _STOP.is_set():
        try:
            result = materialize_meta_portals()
            legacy.set_setting("meta_portal_materialize_last", str(result))
            legacy.set_setting("meta_portal_materialize_error", "")
        except Exception as exc:
            legacy.set_setting("meta_portal_materialize_error", str(exc)[:2000])
            print(f"Meta portal materialization failed: {exc}", flush=True)
        _STOP.wait(MATERIALIZE_INTERVAL_SECONDS)


def install(*, start_background: bool = True) -> None:
    """Start portal materialization after the normal integration runtime is loaded."""
    if not start_background:
        return
    thread = threading.Thread(target=_loop, name="meta-portal-materialize", daemon=True)
    thread.start()
