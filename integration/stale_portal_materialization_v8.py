"""Materialize real Meta portals even when their history is older than the import window.

The normal history importer intentionally mirrors only a bounded number of days into
Chatwoot. That retention policy must not also hide an otherwise valid Messenger or
Marketplace conversation forever. If the bounded importer creates no room link, use
mautrix-meta's read-only bridge database to locate the latest customer-authored
Matrix event for that portal and mirror exactly that one event as a safe anchor.

This keeps the full history window bounded while ensuring every discovered portal
with real customer history can become a Chatwoot conversation.
"""
from __future__ import annotations

import sqlite3
from urllib.parse import quote

import final_app as runtime
import media_context_v3 as media
import runtime_enhancements as enhancements

legacy = runtime.legacy

_base_import_history = enhancements.import_recent_history


def latest_customer_event_id(room_id: str) -> str:
    """Return the newest non-self mautrix-meta message event for a Matrix portal."""
    try:
        with media._meta_db() as conn:
            self_ids = media._self_remote_ids(conn)
            portal = conn.execute(
                "SELECT bridge_id, id, receiver FROM portal WHERE mxid = ? LIMIT 1",
                (room_id,),
            ).fetchone()
            if not portal:
                return ""

            sql = (
                "SELECT mxid FROM message "
                "WHERE bridge_id = ? AND room_id = ? AND room_receiver = ? "
                "AND mxid IS NOT NULL AND mxid != '' "
            )
            params: list[object] = [portal[0], portal[1], portal[2]]
            if self_ids:
                placeholders = ",".join("?" for _ in self_ids)
                sql += f"AND sender_id NOT IN ({placeholders}) "
                params.extend(sorted(self_ids))
            sql += "ORDER BY timestamp DESC, rowid DESC LIMIT 1"
            row = conn.execute(sql, params).fetchone()
    except (OSError, sqlite3.Error):
        return ""
    return str(row[0] or "") if row else ""


def fetch_matrix_event(room_id: str, event_id: str) -> dict:
    encoded_room = quote(room_id, safe="")
    encoded_event = quote(event_id, safe="")
    response = enhancements._matrix_get(
        f"/_matrix/client/v3/rooms/{encoded_room}/event/{encoded_event}",
        timeout=20,
    )
    data = response.json() if response.content else {}
    return data if isinstance(data, dict) else {}


def import_recent_history(room_id: str) -> int:
    """Run bounded history import, then materialize one old customer anchor if needed."""
    imported = _base_import_history(room_id)
    if enhancements.existing_room_link(room_id) is not None:
        return imported

    event_id = latest_customer_event_id(room_id)
    if not event_id or legacy.event_seen(event_id):
        return imported

    try:
        event = fetch_matrix_event(room_id, event_id)
        if event.get("type") != "m.room.message":
            return imported
        if media.message_direction(event) != "incoming":
            return imported
        if media.mirror_matrix_event(room_id, event, history=True):
            print(
                "Meta stale portal materialized from latest customer event "
                f"room={room_id}",
                flush=True,
            )
            return imported + 1
    except Exception as exc:
        print(
            "Meta stale portal materialization failed "
            f"room={room_id}: {type(exc).__name__}: {exc}",
            flush=True,
        )
    return imported


def install() -> None:
    enhancements.import_recent_history = import_recent_history
