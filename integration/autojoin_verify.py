"""Verified auto-join for mautrix-meta portal invitations.

Trust is derived from the installed Matrix application-service registration rather
than guessed MXID prefixes. The appservice bot (`sender_localpart`) is trusted
explicitly and remote-user ghosts are trusted only when they match an *exclusive*
`namespaces.users` entry from that registration.

For this product, trusted Meta portal auto-join is mandatory: Chatwoot must never
require an operator to click Accept in Element. The security boundary is portal
provenance, not an operator-facing enable/disable toggle.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from urllib.parse import quote

import yaml

import final_app as runtime
import runtime_enhancements as enhancements

legacy = runtime.legacy


@dataclass(frozen=True)
class AppserviceTrust:
    bot_mxid: str
    exclusive_user_regexes: tuple[str, ...]


def _membership(room_id: str) -> str:
    path = (
        f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/state/"
        f"m.room.member/{quote(legacy.MATRIX_ADMIN_MXID, safe='')}"
    )
    response = enhancements._matrix_get(path, timeout=15)
    data = response.json() if response.content else {}
    return str(data.get("membership") or "")


def _local_server(mxid: str) -> str:
    if not mxid.startswith("@") or ":" not in mxid:
        return ""
    return mxid.split(":", 1)[1]


def _load_registration() -> dict:
    try:
        with open(legacy.REGISTRATION_PATH, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        print(f"mautrix registration unavailable for invite trust: {exc}", flush=True)
        return {}
    if not isinstance(data, dict):
        print("mautrix registration is not a YAML object; invite trust disabled", flush=True)
        return {}
    return data


def appservice_trust() -> AppserviceTrust:
    """Build the exact trust boundary represented by registration.yaml.

    Matrix AS semantics distinguish *interest* from *ownership*. A non-exclusive
    user namespace can match ordinary Matrix users and therefore is not sufficient
    evidence that mautrix controls the inviter. Only exclusive user namespaces are
    accepted for ghost identities. The appservice sender user is trusted separately,
    as defined by `sender_localpart` in the registration contract.
    """
    data = _load_registration()
    server = legacy.MATRIX_SERVER_NAME
    sender_localpart = str(data.get("sender_localpart") or "").strip()
    bot_mxid = f"@{sender_localpart}:{server}" if sender_localpart else ""

    namespaces = data.get("namespaces") or {}
    users = namespaces.get("users") if isinstance(namespaces, dict) else []
    if not isinstance(users, list):
        users = []

    patterns: list[str] = []
    for entry in users:
        if not isinstance(entry, dict) or entry.get("exclusive") is not True:
            continue
        pattern = str(entry.get("regex") or "").strip()
        if not pattern:
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            print(f"invalid exclusive mautrix user regex ignored pattern={pattern!r}: {exc}", flush=True)
            continue
        patterns.append(pattern)

    return AppserviceTrust(bot_mxid=bot_mxid, exclusive_user_regexes=tuple(dict.fromkeys(patterns)))


def require_registration_access() -> AppserviceTrust:
    """Fail startup when the mounted appservice trust boundary cannot be read.

    Auto-join is mandatory for this product. Running while registration.yaml is
    unreadable only creates a deceptively healthy service that can never accept Meta
    portal invitations, so treat that deployment state as a hard readiness failure.
    """
    trust = appservice_trust()
    if trust.bot_mxid:
        return trust
    path = legacy.REGISTRATION_PATH
    try:
        stat = os.stat(path)
        mode = oct(stat.st_mode & 0o777)
        detail = f"exists owner={stat.st_uid}:{stat.st_gid} mode={mode}"
    except OSError as exc:
        detail = f"stat failed: {exc}"
    raise RuntimeError(
        "mautrix registration trust boundary is unreadable or missing sender_localpart: "
        f"path={path} ({detail})"
    )


def appservice_user_regexes() -> tuple[str, ...]:
    """Compatibility helper used by tests/status code."""
    return appservice_trust().exclusive_user_regexes


def trusted_meta_inviter(mxid: str) -> tuple[bool, str]:
    """Trust only official local identities controlled by this mautrix appservice."""
    if not mxid:
        return False, "missing_inviter"
    if _local_server(mxid) != legacy.MATRIX_SERVER_NAME:
        return False, "non_local_inviter"

    trust = appservice_trust()
    if trust.bot_mxid and mxid == trust.bot_mxid:
        return True, "appservice_sender_localpart"

    for pattern in trust.exclusive_user_regexes:
        try:
            if re.fullmatch(pattern, mxid):
                return True, "exclusive_appservice_user_namespace"
        except re.error:
            continue
    return False, "outside_exclusive_appservice_namespace"


def robust_auto_join_room(room_id: str, room: dict) -> bool:
    """Join a trusted portal invite and verify that membership really persisted.

    There is intentionally no configurable OFF switch here. If a room cannot be
    proven to originate from the installed mautrix-meta appservice it is rejected;
    if it can, the dedicated integration account must accept it automatically.
    """
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
    require_registration_access()
    legacy.set_setting("auto_join_meta_portals", "1")
    enhancements.auto_join_room = robust_auto_join_room
