"""Verified auto-join for mautrix-meta portal invitations.

A successful Matrix /join HTTP response is not treated as sufficient. We confirm the
admin membership state is actually `join` and retry a short bounded number of times.
This closes a room-creation race where Element could continue showing Accept even
though a join request had already returned 200.
"""
from __future__ import annotations

import time
from urllib.parse import quote

import final_app as runtime
import runtime_enhancements as enhancements

legacy = runtime.legacy


def _membership(room_id: str) -> str:
    path = (
        f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/state/"
        f"m.room.member/{quote(legacy.MATRIX_ADMIN_MXID, safe='')}"
    )
    response = enhancements._matrix_get(path, timeout=15)
    data = response.json() if response.content else {}
    return str(data.get("membership") or "")


def robust_auto_join_room(room_id: str, room: dict) -> bool:
    if not enhancements.setting_bool("auto_join_meta_portals", True):
        return False
    inviter = enhancements.invite_sender(room)
    expected = legacy.bridge_bot_mxid()
    if not expected or inviter != expected:
        print(f"matrix invite ignored room={room_id} inviter={inviter or 'unknown'}", flush=True)
        return False

    last_error = None
    for attempt in range(1, 4):
        try:
            enhancements._matrix_post(f"/_matrix/client/v3/join/{quote(room_id, safe='')}")
            membership = _membership(room_id)
            if membership == "join":
                print(f"auto-joined and verified Meta portal room={room_id} attempt={attempt}", flush=True)
                return True
            last_error = RuntimeError(f"join returned but membership is {membership or 'unknown'}")
        except Exception as exc:
            last_error = exc
        time.sleep(0.2 * attempt)

    raise RuntimeError(f"Meta portal auto-join did not persist after 3 attempts: {last_error}")


def install() -> None:
    enhancements.auto_join_room = robust_auto_join_room
