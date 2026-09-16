"""Fail-closed bidirectional Meta <-> Chatwoot conversation deletion."""
from __future__ import annotations

import hashlib
import threading
import time
from urllib.parse import quote

import requests

import final_app as runtime
import meta_portal_reconcile as reconcile
import runtime_enhancements as enhancements

legacy = runtime.legacy
prod = runtime.prod
DELETE_EVENT_TYPE = "com.beeper.delete_chat"
_BOOTSTRAP_LOCK = threading.Lock()
_base_ensure_room_link = None
_base_chatwoot_handler = enhancements.handle_chatwoot_outgoing


def ensure_schema() -> None:
    with legacy.db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS verified_meta_portals (
              room_id TEXT PRIMARY KEY,
              verified_at INTEGER NOT NULL,
              last_seen_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversation_deletions (
              conversation_id INTEGER PRIMARY KEY,
              room_id TEXT NOT NULL,
              origin TEXT NOT NULL CHECK(origin IN ('meta', 'chatwoot')),
              state TEXT NOT NULL,
              matrix_event_id TEXT NOT NULL DEFAULT '',
              attempts INTEGER NOT NULL DEFAULT 0,
              error TEXT NOT NULL DEFAULT '',
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS conversation_deletions_room
              ON conversation_deletions(room_id);
            """
        )


def remember_verified_portal(room_id: str) -> None:
    now = int(time.time())
    with legacy.db() as conn:
        conn.execute(
            "INSERT INTO verified_meta_portals(room_id, verified_at, last_seen_at) VALUES(?, ?, ?) "
            "ON CONFLICT(room_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
            (room_id, now, now),
        )


def portal_was_verified(room_id: str) -> bool:
    with legacy.db() as conn:
        return conn.execute(
            "SELECT 1 FROM verified_meta_portals WHERE room_id=?", (room_id,)
        ).fetchone() is not None


def verify_and_remember_portal(room_id: str) -> bool:
    verified, reason = reconcile.verified_meta_portal(room_id)
    if not verified:
        print(f"conversation lifecycle: portal verification failed room={room_id} reason={reason}", flush=True)
        return False
    remember_verified_portal(room_id)
    return True


def _link_by_room(room_id: str):
    with legacy.db() as conn:
        return conn.execute("SELECT * FROM room_links WHERE room_id=?", (room_id,)).fetchone()


def _link_by_conversation(conversation_id: int):
    with legacy.db() as conn:
        return conn.execute(
            "SELECT * FROM room_links WHERE conversation_id=?", (conversation_id,)
        ).fetchone()


def _operation(conversation_id: int):
    with legacy.db() as conn:
        return conn.execute(
            "SELECT * FROM conversation_deletions WHERE conversation_id=?", (conversation_id,)
        ).fetchone()


def _start_operation(conversation_id: int, room_id: str, origin: str, state: str) -> None:
    now = int(time.time())
    with legacy.db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO conversation_deletions"
            "(conversation_id,room_id,origin,state,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, room_id, origin, state, now, now),
        )


def _update_operation(conversation_id: int, *, state: str, matrix_event_id: str | None = None,
                      error: str | None = None, increment_attempts: bool = False) -> None:
    fields = ["state=?", "updated_at=?"]
    values: list[object] = [state, int(time.time())]
    if matrix_event_id is not None:
        fields.append("matrix_event_id=?")
        values.append(matrix_event_id)
    if error is not None:
        fields.append("error=?")
        values.append(error[:1000])
    if increment_attempts:
        fields.append("attempts=attempts+1")
    values.append(conversation_id)
    with legacy.db() as conn:
        conn.execute(
            f"UPDATE conversation_deletions SET {', '.join(fields)} WHERE conversation_id=?",
            tuple(values),
        )


def _delete_room_link(room_id: str, conversation_id: int) -> None:
    with legacy.db() as conn:
        conn.execute(
            "DELETE FROM room_links WHERE room_id=? AND conversation_id=?",
            (room_id, conversation_id),
        )


def _trusted_bridge_leave(room: dict) -> bool:
    expected_bot = legacy.bridge_bot_mxid()
    if not expected_bot:
        return False
    events = []
    for key in ("state", "timeline"):
        events.extend(((room.get(key) or {}).get("events") or []))
    return any(
        event.get("type") == "m.room.member"
        and event.get("state_key") == legacy.MATRIX_ADMIN_MXID
        and (event.get("content") or {}).get("membership") == "leave"
        and event.get("sender") == expected_bot
        for event in events
    )


def _delete_chatwoot_conversation(conversation_id: int) -> None:
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    response = requests.delete(
        legacy.chatwoot_url(f"/api/v1/accounts/{account_id}/conversations/{conversation_id}"),
        headers=legacy.chatwoot_headers(), timeout=20,
    )
    if response.status_code != 404:
        response.raise_for_status()


def process_matrix_leave(room_id: str, room: dict) -> dict:
    """Propagate only a preverified, bridge-bot-authored portal leave."""
    link = _link_by_room(room_id)
    if not link:
        return {"ok": True, "ignored": True, "reason": "unmapped_room"}
    if not portal_was_verified(room_id):
        return {"ok": True, "ignored": True, "reason": "portal_not_preverified"}
    if not _trusted_bridge_leave(room):
        print(f"conversation lifecycle: untrusted Matrix leave ignored room={room_id}", flush=True)
        return {"ok": True, "ignored": True, "reason": "leave_not_from_bridge_bot"}

    conversation_id = int(link["conversation_id"])
    existing = _operation(conversation_id)
    if existing and existing["origin"] == "chatwoot":
        _delete_room_link(room_id, conversation_id)
        _update_operation(conversation_id, state="completed", error="")
        print(
            f"conversation lifecycle: Chatwoot-origin delete confirmed by Meta room={room_id} conversation={conversation_id}",
            flush=True,
        )
        return {"ok": True, "completed": True, "origin": "chatwoot"}

    _start_operation(conversation_id, room_id, "meta", "remote_confirmed")
    try:
        _delete_chatwoot_conversation(conversation_id)
    except Exception as exc:
        _update_operation(conversation_id, state="failed_retryable", error=str(exc), increment_attempts=True)
        raise
    _delete_room_link(room_id, conversation_id)
    _update_operation(conversation_id, state="completed", error="", increment_attempts=True)
    print(
        f"conversation lifecycle: Meta delete propagated to Chatwoot room={room_id} conversation={conversation_id}",
        flush=True,
    )
    return {"ok": True, "completed": True, "origin": "meta"}


def _configured_scope(payload: dict) -> tuple[bool, str]:
    account = payload.get("account") or {}
    inbox = payload.get("inbox") or {}
    account_id = str(account.get("id") or payload.get("account_id") or "")
    inbox_id = str(inbox.get("id") or payload.get("inbox_id") or "")
    if account_id != str(legacy.get_setting("chatwoot_account_id")):
        return False, "outside_configured_chatwoot_account"
    if inbox_id != str(legacy.get_setting("chatwoot_inbox_id")):
        return False, "outside_configured_chatwoot_inbox"
    return True, ""


def _send_matrix_delete(room_id: str, conversation_id: int) -> str:
    txn = "cwdel-" + hashlib.sha256(f"{conversation_id}:{room_id}".encode()).hexdigest()[:24]
    response = requests.put(
        f"{legacy.MATRIX_HOMESERVER}/_matrix/client/v3/rooms/{quote(room_id, safe='')}/send/"
        f"{DELETE_EVENT_TYPE}/{txn}",
        headers=legacy.matrix_headers(),
        json={"delete_for_everyone": False, "from_message_request": False},
        timeout=20,
    )
    response.raise_for_status()
    event_id = str((response.json() if response.content else {}).get("event_id") or "")
    if not event_id:
        raise RuntimeError("Matrix accepted no event_id for delete request")
    return event_id


def process_chatwoot_delete(payload: dict) -> dict:
    """Translate a signed Chatwoot deletion into BridgeV2's native delete event."""
    in_scope, reason = _configured_scope(payload)
    if not in_scope:
        return {"ok": True, "ignored": True, "reason": reason}
    try:
        conversation_id = int(payload.get("conversation_id") or payload.get("id"))
    except (TypeError, ValueError):
        return {"ok": True, "ignored": True, "reason": "missing_conversation_id"}

    existing = _operation(conversation_id)
    if existing:
        if existing["origin"] == "meta":
            return {"ok": True, "ignored": True, "reason": "meta_delete_loop_suppressed"}
        if existing["state"] in {"remote_requested", "completed"}:
            return {"ok": True, "duplicate": True, "state": existing["state"]}

    link = _link_by_conversation(conversation_id)
    if not link:
        return {"ok": True, "ignored": True, "reason": "unmapped_conversation"}
    room_id = str(link["room_id"])
    if not portal_was_verified(room_id):
        return {"ok": True, "ignored": True, "reason": "portal_not_preverified"}

    _start_operation(conversation_id, room_id, "chatwoot", "pending")
    try:
        event_id = _send_matrix_delete(room_id, conversation_id)
    except Exception as exc:
        _update_operation(conversation_id, state="failed_retryable", error=str(exc), increment_attempts=True)
        raise
    _update_operation(
        conversation_id, state="remote_requested", matrix_event_id=event_id,
        error="", increment_attempts=True,
    )
    print(
        f"conversation lifecycle: Chatwoot delete requested on Meta room={room_id} conversation={conversation_id} event={event_id}",
        flush=True,
    )
    return {"ok": True, "remote_requested": True, "event_id": event_id}


def handle_chatwoot_event(payload: dict) -> dict:
    if payload.get("event") == "conversation_deleted":
        return process_chatwoot_delete(payload)
    return _base_chatwoot_handler(payload)


def lifecycle_sync_once() -> None:
    since = legacy.get_setting("matrix_next_batch")
    params = {"timeout": 25000}
    if since:
        params["since"] = since
    response = requests.get(
        f"{legacy.MATRIX_HOMESERVER}/_matrix/client/v3/sync",
        headers=legacy.matrix_headers(), params=params, timeout=35,
    )
    response.raise_for_status()
    data = response.json()
    next_batch = data.get("next_batch")
    if not since:
        if next_batch:
            legacy.set_setting("matrix_next_batch", next_batch)
        return

    rooms = data.get("rooms") or {}
    for room_id, room in (rooms.get("invite") or {}).items():
        try:
            if enhancements.auto_join_room(room_id, room):
                if verify_and_remember_portal(room_id):
                    imported = enhancements.import_recent_history(room_id)
                    print(f"Meta portal ready room={room_id} imported_history={imported}", flush=True)
        except Exception as exc:
            print(f"matrix auto-join failed room={room_id}: {exc}", flush=True)

    for room_id, room in (rooms.get("join") or {}).items():
        try:
            if not portal_was_verified(room_id):
                verify_and_remember_portal(room_id)
        except Exception as exc:
            print(f"conversation lifecycle: portal registry refresh failed room={room_id}: {exc}", flush=True)
        for event in ((room.get("timeline") or {}).get("events") or []):
            try:
                enhancements.enhanced_live_matrix_event(room_id, event)
            except Exception as exc:
                print(f"matrix event failed room={room_id} event={event.get('event_id')}: {exc}", flush=True)
                return

    for room_id, room in (rooms.get("leave") or {}).items():
        try:
            process_matrix_leave(room_id, room)
        except Exception as exc:
            print(f"conversation lifecycle: Matrix leave propagation failed room={room_id}: {exc}", flush=True)

    if next_batch:
        legacy.set_setting("matrix_next_batch", next_batch)


def verified_ensure_room_link(room_id: str, sender: str):
    row = _base_ensure_room_link(room_id, sender)
    try:
        verify_and_remember_portal(room_id)
    except Exception as exc:
        print(f"conversation lifecycle: failed to register linked portal room={room_id}: {exc}", flush=True)
    return row


def _bootstrap_existing_links() -> None:
    if not _BOOTSTRAP_LOCK.acquire(blocking=False):
        return
    try:
        with legacy.db() as conn:
            room_ids = [str(row["room_id"]) for row in conn.execute("SELECT room_id FROM room_links").fetchall()]
        for room_id in room_ids:
            try:
                verify_and_remember_portal(room_id)
            except Exception as exc:
                print(f"conversation lifecycle: bootstrap verification failed room={room_id}: {exc}", flush=True)
    finally:
        _BOOTSTRAP_LOCK.release()


def install() -> None:
    global _base_ensure_room_link
    ensure_schema()
    legacy.set_setting("repair_deleted_conversations", "0")
    enhancements.repair_deleted_conversation = lambda room_id: False

    _base_ensure_room_link = prod.ensure_room_link
    prod.ensure_room_link = verified_ensure_room_link
    legacy.ensure_room_link = verified_ensure_room_link
    enhancements.handle_chatwoot_outgoing = handle_chatwoot_event
    legacy.sync_once = lifecycle_sync_once

    threading.Thread(
        target=_bootstrap_existing_links,
        name="conversation-lifecycle-bootstrap",
        daemon=True,
    ).start()
