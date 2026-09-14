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


def verified_meta_portal(room_id: str) -> tuple[bool, str]:
    """Verify bridge provenance from room state, not from invite UI text alone."""
    trust = autojoin_verify.appservice_trust()
    if not trust.bot_mxid:
        return False, "registration_has_no_appservice_sender"

    try:
        state = room_admin_state(room_id)
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", "unknown")
        return False, f"room_state_http_{status}"

    for event in state:
        if event.get("type") not in {"m.bridge", "uk.half-shot.bridge"}:
            continue
        content = event.get("content") or {}
        bridgebot = str(content.get("bridgebot") or "").strip()
        if bridgebot == trust.bot_mxid:
            return True, "bridge_state_matches_registered_bot"
    return False, "no_matching_bridge_state"


def _link_exists(room_id: str) -> bool:
    with legacy.db() as conn:
        return conn.execute("SELECT 1 FROM room_links WHERE room_id = ?", (room_id,)).fetchone() is not None


def _join_verified_portal(room_id: str) -> bool:
    """Join after provenance was verified by Synapse admin room state.

    We intentionally do not re-use invite_state sender as the authorization boundary
    here: old invites may be absent from /sync deltas. The room's m.bridge state plus
    the installed appservice registration is the durable source of provenance.
    """
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


def reconcile_meta_portals() -> dict:
    """Repair invited and already-joined Meta portals from authoritative membership state."""
    if not _RECONCILE_LOCK.acquire(blocking=False):
        return {
            "already_running": True,
            "invited": 0,
            "joined": 0,
            "verified_portals": 0,
            "linked": 0,
            "history_imported": 0,
            "ignored": 0,
            "errors": [],
        }
    try:
        # Auto-join is a product invariant. Persist the migration so the admin UI and
        # status surface cannot resurrect an old OFF value from earlier builds.
        legacy.set_setting("auto_join_meta_portals", "1")

        memberships = user_memberships()
        result = {
            "already_running": False,
            "invited": 0,
            "joined": 0,
            "verified_portals": 0,
            "linked": 0,
            "history_imported": 0,
            "ignored": 0,
            "errors": [],
        }

        # Invites first, so a room can immediately participate in the joined-room pass.
        for room_id, membership in memberships.items():
            if membership != "invite":
                continue
            result["invited"] += 1
            try:
                verified, reason = verified_meta_portal(room_id)
                if not verified:
                    result["ignored"] += 1
                    print(f"pending Matrix invite not a verified Meta portal room={room_id} reason={reason}", flush=True)
                    continue
                result["verified_portals"] += 1
                if _join_verified_portal(room_id):
                    result["joined"] += 1
                    memberships[room_id] = "join"
            except Exception as exc:
                result["errors"].append(f"{room_id}: join failed: {exc}")

        if not legacy.configured():
            legacy.set_setting("meta_portal_reconcile_error", "Chatwoot is not configured")
            return result

        # Scan every joined room. This intentionally repairs portals that pre-date the
        # integration checkpoint (notably Marketplace backfill rooms already visible in
        # Element) and therefore never generated a new /sync timeline for our sidecar.
        for room_id, membership in memberships.items():
            if membership != "join":
                continue
            try:
                verified, reason = verified_meta_portal(room_id)
                if not verified:
                    continue
                result["verified_portals"] += 1
                before = _link_exists(room_id)
                imported = enhancements.import_recent_history(room_id)
                result["history_imported"] += imported
                if _link_exists(room_id):
                    result["linked"] += 1
                elif not before and imported == 0:
                    # A legitimate portal with no inbound text in the configured history
                    # window has nothing useful to create in Chatwoot yet. The next live
                    # message will create its conversation normally.
                    pass
            except Exception as exc:
                result["errors"].append(f"{room_id}: reconcile failed: {exc}")

        checked = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        legacy.set_setting("meta_invite_reconcile_at", checked)
        legacy.set_setting("meta_invite_reconcile_joined", str(result["joined"]))
        legacy.set_setting("meta_portal_reconcile_verified", str(result["verified_portals"]))
        legacy.set_setting("meta_portal_reconcile_linked", str(result["linked"]))
        legacy.set_setting("meta_portal_reconcile_history", str(result["history_imported"]))
        legacy.set_setting("meta_portal_reconcile_error", " | ".join(result["errors"])[:2000])
        return result
    finally:
        _RECONCILE_LOCK.release()


def _loop() -> None:
    # Let the normal /sync fast path initialize first, then continuously repair any
    # invite/backfill state it missed. This is deliberately low-frequency; live events
    # should normally be handled immediately by /sync.
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

    # Keep the existing settings API backward compatible while making the old toggle
    # non-authoritative. In this product, disabling auto-join would make Chatwoot depend
    # on a human operating Element, which is not an acceptable mode.
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
        # Existing Advanced button now performs a full authoritative reconcile rather
        # than only inspecting the current /sync invite snapshot.
        admin_module.reconcile_pending_meta_invites = reconcile_meta_portals

    if start_background and not any(t.name == "meta-portal-reconcile" and t.is_alive() for t in threading.enumerate()):
        threading.Thread(target=_loop, name="meta-portal-reconcile", daemon=True).start()
