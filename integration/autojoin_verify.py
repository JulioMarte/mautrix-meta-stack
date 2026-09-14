"""Verified auto-join for mautrix-meta portal invitations.

A successful Matrix /join HTTP response is not treated as sufficient. We confirm the
admin membership state is actually `join` and retry a short bounded number of times.
Portal rooms may be created/invited by the bridge bot *or* by a bridge-controlled
Meta ghost user, so trust is derived from the appservice registration user namespace
instead of assuming every portal invite sender is the bot MXID.
"""
from __future__ import annotations

import re
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


def _yaml_scalar(value: str) -> str:
    """Decode the simple quoted scalars emitted in mautrix appservice registrations."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        quote_char = value[0]
        value = value[1:-1]
        if quote_char == "'":
            value = value.replace("''", "'")
        else:
            # Registration regexes only need the common YAML/JSON escapes here.
            value = value.replace('\\"', '"').replace('\\\\', '\\')
    return value


def appservice_user_regexes() -> tuple[str, ...]:
    """Return user-MXID regexes from the installed mautrix-meta registration.

    The registration is the authoritative ownership boundary Synapse itself uses for
    application-service users. If it cannot be parsed, callers fail closed and only
    the explicit bridge bot MXID remains trusted.
    """
    patterns: list[str] = []
    in_users = False
    try:
        with open(legacy.REGISTRATION_PATH, "r", encoding="utf-8") as fh:
            for raw in fh:
                stripped = raw.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped == "users:":
                    in_users = True
                    continue
                if in_users and stripped in {"aliases:", "rooms:"}:
                    in_users = False
                    continue
                if not in_users:
                    continue
                # Generated registrations use either `regex:` or `- regex:`.
                candidate = stripped[2:].strip() if stripped.startswith("- ") else stripped
                if candidate.startswith("regex:"):
                    pattern = _yaml_scalar(candidate.split(":", 1)[1])
                    if pattern:
                        patterns.append(pattern)
    except OSError as exc:
        print(f"mautrix registration unavailable for invite trust: {exc}", flush=True)
        return ()
    return tuple(dict.fromkeys(patterns))


def trusted_meta_inviter(mxid: str) -> tuple[bool, str]:
    """Trust the bot or any user MXID owned by the mautrix-meta appservice."""
    expected_bot = legacy.bridge_bot_mxid()
    if expected_bot and mxid == expected_bot:
        return True, "bridge_bot"
    if not mxid:
        return False, "missing_inviter"
    for pattern in appservice_user_regexes():
        try:
            if re.fullmatch(pattern, mxid):
                return True, "appservice_user_namespace"
        except re.error as exc:
            print(f"invalid mautrix registration user regex ignored pattern={pattern!r}: {exc}", flush=True)
    return False, "outside_appservice_user_namespace"


def robust_auto_join_room(room_id: str, room: dict) -> bool:
    if not enhancements.setting_bool("auto_join_meta_portals", True):
        return False
    inviter = enhancements.invite_sender(room)
    trusted, trust_reason = trusted_meta_inviter(inviter)
    if not trusted:
        print(
            f"matrix invite ignored room={room_id} inviter={inviter or 'unknown'} reason={trust_reason}",
            flush=True,
        )
        return False

    last_error = None
    for attempt in range(1, 4):
        try:
            enhancements._matrix_post(f"/_matrix/client/v3/join/{quote(room_id, safe='')}")
            membership = _membership(room_id)
            if membership == "join":
                print(
                    "auto-joined and verified Meta portal "
                    f"room={room_id} inviter={inviter} trust={trust_reason} attempt={attempt}",
                    flush=True,
                )
                return True
            last_error = RuntimeError(f"join returned but membership is {membership or 'unknown'}")
        except Exception as exc:
            last_error = exc
        time.sleep(0.2 * attempt)

    raise RuntimeError(f"Meta portal auto-join did not persist after 3 attempts: {last_error}")


def install() -> None:
    enhancements.auto_join_room = robust_auto_join_room
