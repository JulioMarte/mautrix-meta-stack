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
from meta_provisioning import MautrixProvisioningClient, connection_summary

legacy = runtime.legacy
prod = runtime.prod
DELETE_EVENT_TYPE = "com.beeper.delete_chat"
_BOOTSTRAP_LOCK = threading.Lock()
_RECONCILE_LOCK = threading.RLock()
_base_ensure_room_link = None
_base_chatwoot_handler = enhancements.handle_chatwoot_outgoing

META_RETRY_BASE_SECONDS = 30
META_RETRY_MAX_SECONDS = 3600
META_RETRY_BATCH_SIZE = 20
CHATWOOT_RETRY_BATCH_SIZE = 10
CHATWOOT_LOGIN_RETRY_SECONDS = 60
META_LOGIN_CACHE_SECONDS = 15
_META_LOGIN_CACHE_LOCK = threading.Lock()
_META_LOGIN_CACHE = {"checked_at": 0.0, "available": False, "reason": "not_checked"}

_ALLOWED_TRANSITIONS = {
    "pending": {"remote_requested", "failed_retryable", "completed"},
    "remote_requested": {"completed"},
    "remote_confirmed": {"completed", "failed_retryable"},
    "failed_retryable": {"failed_retryable", "remote_requested", "completed"},
    "completed": set(),
}


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


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
              next_retry_at INTEGER NOT NULL DEFAULT 0,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS conversation_deletions_room
              ON conversation_deletions(room_id);
            """
        )
        _ensure_column(conn, "conversation_deletions", "next_retry_at", "INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS conversation_deletions_retry "
            "ON conversation_deletions(origin, state, next_retry_at)"
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


def _validate_transition(current_state: str, new_state: str, conversation_id: int) -> None:
    if new_state not in _ALLOWED_TRANSITIONS:
        raise ValueError(f"unknown conversation deletion state: {new_state}")
    if new_state != current_state and new_state not in _ALLOWED_TRANSITIONS.get(current_state, set()):
        raise RuntimeError(
            f"invalid conversation deletion transition {current_state}->{new_state} for {conversation_id}"
        )


def _start_operation(conversation_id: int, room_id: str, origin: str, state: str) -> None:
    if state not in _ALLOWED_TRANSITIONS:
        raise ValueError(f"unknown conversation deletion state: {state}")
    now = int(time.time())
    with legacy.db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO conversation_deletions"
            "(conversation_id,room_id,origin,state,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, room_id, origin, state, now, now),
        )


def _update_operation(conversation_id: int, *, state: str, matrix_event_id: str | None = None,
                      error: str | None = None, increment_attempts: bool = False,
                      next_retry_at: int | None = None) -> None:
    current = _operation(conversation_id)
    if not current:
        raise RuntimeError(f"conversation deletion operation missing: {conversation_id}")
    _validate_transition(str(current["state"]), state, conversation_id)

    fields = ["state=?", "updated_at=?"]
    values: list[object] = [state, int(time.time())]
    if matrix_event_id is not None:
        fields.append("matrix_event_id=?")
        values.append(matrix_event_id)
    if error is not None:
        fields.append("error=?")
        values.append(error[:1000])
    if next_retry_at is not None:
        fields.append("next_retry_at=?")
        values.append(int(next_retry_at))
    if increment_attempts:
        fields.append("attempts=attempts+1")
    values.append(conversation_id)
    with legacy.db() as conn:
        conn.execute(
            f"UPDATE conversation_deletions SET {', '.join(fields)} WHERE conversation_id=?",
            tuple(values),
        )


def _retry_delay_seconds(attempt_number: int) -> int:
    exponent = max(0, min(16, int(attempt_number) - 1))
    return min(META_RETRY_MAX_SECONDS, META_RETRY_BASE_SECONDS * (2 ** exponent))


def _mark_retryable(conversation_id: int, exc: Exception) -> None:
    operation = _operation(conversation_id)
    attempts = int(operation["attempts"]) if operation else 0
    retry_at = int(time.time()) + _retry_delay_seconds(attempts + 1)
    _update_operation(
        conversation_id,
        state="failed_retryable",
        error=str(exc),
        increment_attempts=True,
        next_retry_at=retry_at,
    )


def _meta_login_available(*, force: bool = False) -> tuple[bool, str]:
    """Return whether BridgeV2 currently has a usable Meta login.

    The result is cached briefly so a batch of stale Chatwoot mappings produces
    one provisioning status request rather than one request per conversation.
    Failures are treated as unavailable: deletion intent stays durable and is
    retried later instead of sending an event that mautrix-meta cannot execute.
    """
    now = time.monotonic()
    with _META_LOGIN_CACHE_LOCK:
        age = now - float(_META_LOGIN_CACHE["checked_at"])
        if not force and age >= 0 and age < META_LOGIN_CACHE_SECONDS:
            return bool(_META_LOGIN_CACHE["available"]), str(_META_LOGIN_CACHE["reason"])

        try:
            summary = connection_summary(MautrixProvisioningClient().whoami())
            available = bool(summary.get("connected"))
            reason = str(summary.get("status") or ("connected" if available else "disconnected"))
        except Exception as exc:
            available = False
            reason = f"status_unavailable:{type(exc).__name__}"

        _META_LOGIN_CACHE.update(
            checked_at=now,
            available=available,
            reason=reason,
        )
        return available, reason


def _defer_chatwoot_delete(conversation_id: int, reason: str) -> None:
    _update_operation(
        conversation_id,
        state="failed_retryable",
        error=f"Meta login unavailable: {reason}",
        increment_attempts=False,
        next_retry_at=int(time.time()) + CHATWOOT_LOGIN_RETRY_SECONDS,
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


def _finalize_meta_delete(conversation_id: int, room_id: str) -> None:
    """Atomically mark local completion and remove the mapping after Chatwoot is gone."""
    now = int(time.time())
    with legacy.db() as conn:
        current = conn.execute(
            "SELECT state FROM conversation_deletions WHERE conversation_id=?", (conversation_id,)
        ).fetchone()
        if not current:
            raise RuntimeError(f"conversation deletion operation missing: {conversation_id}")
        _validate_transition(str(current["state"]), "completed", conversation_id)
        conn.execute(
            "UPDATE conversation_deletions SET state='completed', error='', next_retry_at=0, "
            "attempts=attempts+1, updated_at=? WHERE conversation_id=?",
            (now, conversation_id),
        )
        conn.execute(
            "DELETE FROM room_links WHERE room_id=? AND conversation_id=?",
            (room_id, conversation_id),
        )


def _complete_meta_delete(conversation_id: int, room_id: str) -> None:
    # External side effect first, then one SQLite transaction. If the process dies
    # between them, remote_confirmed/failed_retryable remains durable and a retry
    # will receive Chatwoot 404 and finalize idempotently.
    _delete_chatwoot_conversation(conversation_id)
    _finalize_meta_delete(conversation_id, room_id)


def reconcile_retryable_meta_deletions(*, limit: int = META_RETRY_BATCH_SIZE,
                                       now: int | None = None) -> dict:
    """Recover confirmed Meta deletes independently of Matrix sync token delivery."""
    if not _RECONCILE_LOCK.acquire(blocking=False):
        return {"processed": 0, "completed": 0, "failed": 0, "busy": True}
    try:
        current_time = int(time.time()) if now is None else int(now)
        with legacy.db() as conn:
            rows = conn.execute(
                "SELECT * FROM conversation_deletions "
                "WHERE origin='meta' AND (state='remote_confirmed' OR "
                "(state='failed_retryable' AND next_retry_at<=?)) "
                "ORDER BY CASE state WHEN 'remote_confirmed' THEN 0 ELSE 1 END, "
                "next_retry_at ASC, updated_at ASC LIMIT ?",
                (current_time, max(1, int(limit))),
            ).fetchall()

        completed = 0
        failed = 0
        for row in rows:
            conversation_id = int(row["conversation_id"])
            room_id = str(row["room_id"])
            try:
                _complete_meta_delete(conversation_id, room_id)
                completed += 1
                print(
                    f"conversation lifecycle: reconciliation completed Meta->Chatwoot delete "
                    f"room={room_id} conversation={conversation_id}",
                    flush=True,
                )
            except Exception as exc:
                failed += 1
                _mark_retryable(conversation_id, exc)
                print(
                    f"conversation lifecycle: reconciliation failed Meta->Chatwoot delete "
                    f"room={room_id} conversation={conversation_id}: {exc}",
                    flush=True,
                )
        return {"processed": len(rows), "completed": completed, "failed": failed}
    finally:
        _RECONCILE_LOCK.release()


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
        _update_operation(conversation_id, state="completed", error="", next_retry_at=0)
        print(
            f"conversation lifecycle: Chatwoot-origin delete confirmed by Meta room={room_id} conversation={conversation_id}",
            flush=True,
        )
        return {"ok": True, "completed": True, "origin": "chatwoot"}

    with _RECONCILE_LOCK:
        _start_operation(conversation_id, room_id, "meta", "remote_confirmed")
        existing = _operation(conversation_id)
        if existing and existing["origin"] != "meta":
            raise RuntimeError(
                f"conversation lifecycle origin conflict conversation={conversation_id} "
                f"origin={existing['origin']}"
            )
        if existing and existing["state"] == "completed":
            _delete_room_link(room_id, conversation_id)
            return {"ok": True, "completed": True, "origin": "meta", "duplicate": True}
        try:
            _complete_meta_delete(conversation_id, room_id)
        except Exception as exc:
            _mark_retryable(conversation_id, exc)
            raise

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


def reconcile_retryable_chatwoot_deletions(*, limit: int = CHATWOOT_RETRY_BATCH_SIZE,
                                           now: int | None = None) -> dict:
    """Resume Chatwoot-origin deletes without flooding Matrix while Meta is offline."""
    current_time = int(time.time()) if now is None else int(now)
    with legacy.db() as conn:
        rows = conn.execute(
            "SELECT * FROM conversation_deletions "
            "WHERE origin='chatwoot' AND state='failed_retryable' AND next_retry_at<=? "
            "ORDER BY next_retry_at ASC, updated_at ASC LIMIT ?",
            (current_time, max(1, int(limit))),
        ).fetchall()

    if not rows:
        return {"processed": 0, "requested": 0, "deferred": 0, "failed": 0}

    available, reason = _meta_login_available(force=True)
    if not available:
        retry_at = current_time + CHATWOOT_LOGIN_RETRY_SECONDS
        for row in rows:
            _update_operation(
                int(row["conversation_id"]),
                state="failed_retryable",
                error=f"Meta login unavailable: {reason}",
                next_retry_at=retry_at,
            )
        print(
            f"conversation lifecycle: deferred {len(rows)} Chatwoot->Meta deletes; "
            f"Meta login unavailable reason={reason}",
            flush=True,
        )
        return {"processed": len(rows), "requested": 0, "deferred": len(rows), "failed": 0}

    requested = 0
    failed = 0
    for row in rows:
        conversation_id = int(row["conversation_id"])
        room_id = str(row["room_id"])
        if not _link_by_conversation(conversation_id):
            continue
        try:
            event_id = _send_matrix_delete(room_id, conversation_id)
            existing = _operation(conversation_id)
            if existing and existing["state"] == "completed":
                continue
            _update_operation(
                conversation_id,
                state="remote_requested",
                matrix_event_id=event_id,
                error="",
                increment_attempts=True,
                next_retry_at=0,
            )
            requested += 1
            print(
                f"conversation lifecycle: resumed Chatwoot delete on Meta "
                f"room={room_id} conversation={conversation_id} event={event_id}",
                flush=True,
            )
        except Exception as exc:
            failed += 1
            _mark_retryable(conversation_id, exc)

    return {
        "processed": len(rows),
        "requested": requested,
        "deferred": 0,
        "failed": failed,
    }


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
        if existing["state"] == "failed_retryable":
            retry_at = int(existing["next_retry_at"] or 0)
            now = int(time.time())
            if retry_at > now:
                return {
                    "ok": True,
                    "deferred": True,
                    "reason": "retry_scheduled",
                    "retry_at": retry_at,
                }

    link = _link_by_conversation(conversation_id)
    if not link:
        return {"ok": True, "ignored": True, "reason": "unmapped_conversation"}
    room_id = str(link["room_id"])
    if not portal_was_verified(room_id):
        return {"ok": True, "ignored": True, "reason": "portal_not_preverified"}

    _start_operation(conversation_id, room_id, "chatwoot", "pending")

    available, unavailable_reason = _meta_login_available()
    if not available:
        _defer_chatwoot_delete(conversation_id, unavailable_reason)
        return {
            "ok": True,
            "deferred": True,
            "reason": "meta_login_unavailable",
            "retry_after_seconds": CHATWOOT_LOGIN_RETRY_SECONDS,
        }

    try:
        event_id = _send_matrix_delete(room_id, conversation_id)
    except Exception as exc:
        existing = _operation(conversation_id)
        if existing and existing["state"] == "completed":
            return {"ok": True, "completed": True, "origin": "chatwoot", "confirmed_during_submit": True}
        _mark_retryable(conversation_id, exc)
        raise

    existing = _operation(conversation_id)
    if existing and existing["state"] == "completed":
        return {"ok": True, "completed": True, "origin": "chatwoot", "confirmed_during_submit": True}
    _update_operation(
        conversation_id,
        state="remote_requested",
        matrix_event_id=event_id,
        error="",
        increment_attempts=True,
        next_retry_at=0,
    )
    print(
        f"conversation lifecycle: Chatwoot delete requested on Meta room={room_id} conversation={conversation_id} event={event_id}",
        flush=True,
    )
    return {"ok": True, "remote_requested": True, "event_id": event_id}


def recover_missing_chatwoot_conversation(room_id: str, conversation_id: int) -> dict:
    """Recover a Chatwoot deletion whose webhook was missed.

    A 404 observed while syncing operator context is authoritative only for the
    exact persisted room/conversation mapping. Reuse the normal Chatwoot-origin
    deletion state machine so we never recreate an intentionally deleted chat and
    never bypass portal verification or loop suppression.
    """
    try:
        conversation_id = int(conversation_id)
    except (TypeError, ValueError):
        return {"ok": True, "ignored": True, "reason": "invalid_conversation_id"}

    link = _link_by_conversation(conversation_id)
    if not link or str(link["room_id"]) != str(room_id):
        return {"ok": True, "ignored": True, "reason": "stale_mapping_changed"}

    return process_chatwoot_delete({
        "event": "conversation_deleted",
        "conversation_id": conversation_id,
        "account": {"id": int(legacy.get_setting("chatwoot_account_id"))},
        "inbox": {"id": int(legacy.get_setting("chatwoot_inbox_id"))},
    })


def handle_chatwoot_event(payload: dict) -> dict:
    if payload.get("event") == "conversation_deleted":
        return process_chatwoot_delete(payload)
    return _base_chatwoot_handler(payload)


def lifecycle_sync_once() -> None:
    try:
        reconcile_retryable_meta_deletions()
    except Exception as exc:
        print(f"conversation lifecycle: retry reconciliation failed: {exc}", flush=True)
    try:
        reconcile_retryable_chatwoot_deletions()
    except Exception as exc:
        print(f"conversation lifecycle: Chatwoot retry reconciliation failed: {exc}", flush=True)

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
        try:
            reconcile_retryable_meta_deletions()
        except Exception as exc:
            print(f"conversation lifecycle: bootstrap retry reconciliation failed: {exc}", flush=True)
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
    enhancements.handle_missing_chatwoot_conversation = recover_missing_chatwoot_conversation
    legacy.sync_once = lifecycle_sync_once

    threading.Thread(
        target=_bootstrap_existing_links,
        name="conversation-lifecycle-bootstrap",
        daemon=True,
    ).start()
