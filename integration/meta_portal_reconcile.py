"""Authoritative Meta portal reconciliation for the Chatwoot-only product flow.

mautrix-meta intentionally invites a normal Matrix user into newly-created portal
rooms. Matrix does not allow another user/appservice to force that human user into
the room; the invited user must join it. This service owns that acceptance on behalf
of the dedicated integration Matrix account.

Live /sync invite handling remains the fast path. This module adds an authoritative
reconciliation path using Synapse's server-admin APIs so missed/old invitations and
already-joined Marketplace portals cannot remain stranded behind a stale sync token.
"""
from __future__ import annotations

import threading
import time
from urllib.parse import quote

import requests

import final_app as runtime
import runtime_enhancements as enhancements
import autojoin_verify

legacy = runtime.legacy

RECONCILE_INTERVAL_SECONDS = 30
_RECONCILE_LOCK = threading.Lock()
_STOP = threading.Event()


def _utc_now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())


def _admin_get(path: str, *, timeout: int = 20) -> dict:
    response = requests.get(
        f"{legacy.MATRIX_HOMESERVER}{path}",
        headers=legacy.matrix_headers(),
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json() if response.content else {}


def user_memberships() -> dict[str, str]:
    encoded_user = quote(legacy.MATRIX_ADMIN_MXID, safe="")
    data = _admin_get(f"/_synapse/admin/v1/users/{encoded_user}/memberships")
    memberships = data.get("memberships") if isinstance(data, dict) else None
    if not isinstance(memberships, dict):
        raise RuntimeError("Synapse memberships API returned an invalid response")
    return {str(room_id): str(membership) for room_id, membership in memberships.items()}


def room_admin_state(room_id: str) -> list[dict]:
    encoded_room = quote(room_id, safe="")
    data = _admin_get(f"/_synapse/admin/v1/rooms/{encoded_room}/state")
    state = data.get("state") if isinstance(data, dict) else None
    if not isinstance(state, list):
        raise RuntimeError(f"Synapse room-state API returned an invalid response for {room_id}")
    return [event for event in state if isinstance(event, dict)]


def _trusted_pending_invite_from_state(state: list[dict]) -> tuple[bool, str]:
    """Verify the current admin invite directly from authoritative room state.

    This is the important reconciliation fallback for portal rooms that do not yet
    expose an m.bridge event through Synapse's current-state view. The invitation
    itself is still a durable Matrix state event: if the current membership for the
    dedicated integration account is ``invite`` and the event sender is the installed
    mautrix appservice bot/ghost, that is sufficient provenance to auto-join.
    """
    for event in state:
        if event.get("type") != "m.room.member":
            continue
        if event.get("state_key") != legacy.MATRIX_ADMIN_MXID:
            continue
        if (event.get("content") or {}).get("membership") != "invite":
            continue
        inviter = str(event.get("sender") or "")
        trusted, reason = autojoin_verify.trusted_meta_inviter(inviter)
        if trusted:
            return True, f"trusted_invite_membership:{reason}"
        return False, f"untrusted_invite_membership:{reason}"
    return False, "no_current_admin_invite_membership"


def verified_meta_portal(room_id: str, *, allow_pending_invite: bool = False) -> tuple[bool, str]:
    """Verify bridge provenance from authoritative Synapse room state.

    Joined/backfill rooms are verified by their m.bridge metadata. Pending invitations
    get one additional proof path: the current m.room.member invite for our dedicated
    integration user may itself have been sent by the registered mautrix appservice.
    That is stronger and more reliable than requiring m.bridge metadata to be visible
    before the invited account has joined.
    """
    trust = autojoin_verify.appservice_trust()
    if not trust.bot_mxid:
        return False, "registration_has_no_appservice_sender"

    try:
        state = room_admin_state(room_id)
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", "unknown")
        return False, f"room_state_http_{status}"

    if allow_pending_invite:
        verified, reason = _trusted_pending_invite_from_state(state)
        if verified:
            return True, reason

    for event in state:
        if event.get("type") not in {"m.bridge", "uk.half-shot.bridge"}:
            continue
        content = event.get("content") or {}
        bridgebot = str(content.get("bridgebot") or "").strip()
        if bridgebot == trust.bot_mxid:
            return True, "bridge_state_matches_registered_bot"

    if allow_pending_invite:
        _, invite_reason = _trusted_pending_invite_from_state(state)
        return False, f"no_matching_bridge_state;{invite_reason}"
    return False, "no_matching_bridge_state"


def _link_exists(room_id: str) -> bool:
    with legacy.db() as conn:
        return conn.execute("SELECT 1 FROM room_links WHERE room_id = ?", (room_id,)).fetchone() is not None


def _join_verified_portal(room_id: str) -> bool:
    """Join after provenance was verified by Synapse admin room state."""
    last_error = None
    encoded_room = quote(room_id, safe="")
    for attempt in range(1, 4):
        try:
            enhancements._matrix_post(f"/_matrix/client/v3/join/{encoded_room}")
            membership = autojoin_verify._membership(room_id)
            if membership == "join":
                print(f"reconciled Meta portal join room={room_id} attempt={attempt}", flush=True)
                return True
            last_error = RuntimeError(f"membership remained {membership or 'unknown'}")
        except Exception as exc:
            last_error = exc
        time.sleep(0.2 * attempt)
    raise RuntimeError(f"verified Meta portal join did not persist: {last_error}")


def _empty_result(*, already_running: bool = False) -> dict:
    return {
        "checked_at": _utc_now(),
        "already_running": already_running,
        "invited": 0,
        "joined": 0,
        "verified_portals": 0,
        "linked": 0,
        "history_imported": 0,
        "ignored": 0,
        "errors": [],
    }


def reconcile_meta_portals() -> dict:
    """Repair invited and already-joined Meta portals from authoritative membership state."""
    if not _RECONCILE_LOCK.acquire(blocking=False):
        return _empty_result(already_running=True)
    try:
        legacy.set_setting("auto_join_meta_portals", "1")

        memberships = user_memberships()
        result = _empty_result()
        verified_rooms: set[str] = set()

        # Invites first. For pending rooms, trust the authoritative current membership
        # event when its sender belongs to the installed mautrix appservice. Requiring
        # m.bridge here stranded real portals in Element on deployments where bridge
        # metadata was not visible until after the user joined.
        for room_id, membership in list(memberships.items()):
            if membership != "invite":
                continue
            result["invited"] += 1
            try:
                verified, reason = verified_meta_portal(room_id, allow_pending_invite=True)
                if not verified:
                    result["ignored"] += 1
                    print(f"pending Matrix invite not a verified Meta portal room={room_id} reason={reason}", flush=True)
                    continue
                verified_rooms.add(room_id)
                if _join_verified_portal(room_id):
                    result["joined"] += 1
                    memberships[room_id] = "join"
            except Exception as exc:
                result["errors"].append(f"{room_id}: join failed: {exc}")

        if not legacy.configured():
            result["verified_portals"] = len(verified_rooms)
            result["checked_at"] = _utc_now()
            legacy.set_setting("meta_invite_reconcile_at", result["checked_at"])
            legacy.set_setting("meta_portal_reconcile_error", "Chatwoot is not configured")
            return result

        # Scan every joined room. This intentionally repairs portals that pre-date the
        # integration checkpoint (notably Marketplace backfill rooms already visible in
        # Element) and therefore never generated a new /sync timeline for our sidecar.
        # Once joined, require durable bridge metadata; the old invite event is no longer
        # current state and must not be guessed from room names or presentation data.
        for room_id, membership in memberships.items():
            if membership != "join":
                continue
            try:
                if room_id not in verified_rooms:
                    verified, reason = verified_meta_portal(room_id)
                    if not verified:
                        continue
                    verified_rooms.add(room_id)
                before = _link_exists(room_id)
                imported = enhancements.import_recent_history(room_id)
                result["history_imported"] += imported
                if _link_exists(room_id):
                    result["linked"] += 1
                elif not before and imported == 0:
                    pass
            except Exception as exc:
                result["errors"].append(f"{room_id}: reconcile failed: {exc}")

        result["verified_portals"] = len(verified_rooms)
        result["checked_at"] = _utc_now()
        legacy.set_setting("meta_invite_reconcile_at", result["checked_at"])
        legacy.set_setting("meta_invite_reconcile_joined", str(result["joined"]))
        legacy.set_setting("meta_portal_reconcile_verified", str(result["verified_portals"]))
        legacy.set_setting("meta_portal_reconcile_linked", str(result["linked"]))
        legacy.set_setting("meta_portal_reconcile_history", str(result["history_imported"]))
        legacy.set_setting("meta_portal_reconcile_error", " | ".join(result["errors"])[:2000])
        return result
    finally:
        _RECONCILE_LOCK.release()


def _loop() -> None:
    _STOP.wait(3)
    while not _STOP.is_set():
        try:
            reconcile_meta_portals()
        except Exception as exc:
            legacy.set_setting("meta_portal_reconcile_error", str(exc)[:2000])
            print(f"Meta portal reconciliation failed: {exc}", flush=True)
        _STOP.wait(RECONCILE_INTERVAL_SECONDS)


def install(admin_module=None, *, start_background: bool = True) -> None:
    """Install mandatory auto-join semantics and authoritative reconciliation."""
    legacy.set_setting("auto_join_meta_portals", "1")

    original_save = enhancements.save_operations_settings
    original_state = enhancements.operations_state

    if not getattr(original_save, "_meta_autojoin_mandatory", False):
        def save_required_operations(*, auto_join: bool, import_history: bool, history_limit: int,
                                     sync_profiles: bool, repair_deleted: bool) -> None:
            original_save(
                auto_join=True,
                import_history=import_history,
                history_limit=history_limit,
                sync_profiles=sync_profiles,
                repair_deleted=repair_deleted,
            )
            legacy.set_setting("auto_join_meta_portals", "1")
        save_required_operations._meta_autojoin_mandatory = True
        enhancements.save_operations_settings = save_required_operations

        def required_operations_state() -> dict:
            state = original_state()
            state["auto_join"] = True
            return state
        enhancements.operations_state = required_operations_state

    if admin_module is not None:
        admin_module.reconcile_pending_meta_invites = reconcile_meta_portals

    if start_background and not any(t.name == "meta-portal-reconcile" and t.is_alive() for t in threading.enumerate()):
        threading.Thread(target=_loop, name="meta-portal-reconcile", daemon=True).start()
