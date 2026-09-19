#!/usr/bin/env python3
"""Live Docker journey for contact-profile and sync-policy reconciliation.

This runs inside the real integration container against the real Synapse used by
CI. Chatwoot is represented by a local HTTP double. The journey validates the
production-composed profile path (Matrix profile + MXC download + multipart
Chatwoot update), verifies generation-scoped dedupe is untouched, and verifies a
history-policy change requests and completes reconciliation without replaying a
previously delivered event.
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import requests

# Do not start a second Matrix /sync owner in this helper process. We still import
# the exact production composition below so all wrappers/schema are installed.
os.environ["START_MATRIX_SYNC"] = "false"
import runtime_entrypoint  # noqa: F401,E402
import binding_generations_v12 as bindings  # noqa: E402
import delivery_history_v2 as history  # noqa: E402
import final_app as runtime  # noqa: E402

legacy = runtime.legacy
MATRIX = os.environ.get("MATRIX_HOMESERVER", "http://synapse:8008").rstrip("/")
ADMIN_MXID = os.environ["MATRIX_ADMIN_MXID"]
ADMIN_PASSWORD = os.environ["MATRIX_ADMIN_PASSWORD"]
ACCOUNT_ID = 801
INBOX_ID = 802
CONTACT_ID = 803
CONVERSATION_ID = 804
PORT = 8092
AVATAR_BYTES = b"ci-avatar-bytes-profile-reconciliation"


def fail(message: str) -> None:
    raise AssertionError(message)


def wait_until(predicate, message: str, timeout: float = 30.0, interval: float = 0.25):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(interval)
    fail(f"timeout: {message}; last={last!r}")


def admin_login() -> str:
    response = requests.post(
        MATRIX + "/_matrix/client/v3/login",
        json={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": ADMIN_MXID},
            "password": ADMIN_PASSWORD,
        },
        timeout=10,
    )
    response.raise_for_status()
    token = str((response.json() or {}).get("access_token") or "")
    if not token:
        fail("Synapse login did not return an access token")
    return token


def matrix_put(token: str, path: str, payload: dict) -> None:
    response = requests.put(
        MATRIX + path,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=10,
    )
    if response.status_code >= 400:
        fail(f"Matrix PUT {path} -> {response.status_code}: {response.text[:500]}")


def upload_avatar(token: str) -> str:
    response = requests.post(
        MATRIX + "/_matrix/media/v3/upload",
        params={"filename": "ci-avatar.png"},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "image/png"},
        data=AVATAR_BYTES,
        timeout=10,
    )
    if response.status_code >= 400:
        # Synapse keeps the legacy media endpoint for compatibility.
        response = requests.post(
            MATRIX + "/_matrix/client/v1/media/upload",
            params={"filename": "ci-avatar.png"},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "image/png"},
            data=AVATAR_BYTES,
            timeout=10,
        )
    response.raise_for_status()
    mxc = str((response.json() or {}).get("content_uri") or "")
    if not mxc.startswith("mxc://"):
        fail(f"Matrix media upload returned invalid content_uri: {mxc!r}")
    return mxc


class ChatwootDouble(BaseHTTPRequestHandler):
    profile_updates: list[dict] = []

    def do_GET(self):  # noqa: N802
        if self.path == f"/api/v1/accounts/{ACCOUNT_ID}/inboxes/{INBOX_ID}":
            body = json.dumps({
                "id": INBOX_ID,
                "name": "CI reconciliation inbox",
                "channel_type": "Channel::Api",
                "identifier": "ci-profile-reconcile-api",
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_PUT(self):  # noqa: N802
        if self.path != f"/api/v1/accounts/{ACCOUNT_ID}/contacts/{CONTACT_ID}":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length)
        self.__class__.profile_updates.append({
            "content_type": self.headers.get("Content-Type", ""),
            "body": body,
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, fmt, *args):
        print("mock-chatwoot-reconcile: " + (fmt % args), flush=True)


def seed_projection(binding) -> int:
    now = int(time.time())
    room_id = "!profile-reconcile-e2e:matrix.example.com"
    with legacy.db() as conn:
        iid = bindings._integration_id(conn)
        cur = conn.execute(
            "INSERT INTO external_identities"
            "(integration_id,bridge_id,portal_id,portal_receiver,matrix_room_id,"
            "first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?)",
            (iid, "meta", "ci-profile-thread", "ci-receiver", room_id, now, now),
        )
        external_id = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO conversation_bindings"
            "(chatwoot_binding_id,external_identity_id,matrix_room_id,chatwoot_contact_id,"
            "chatwoot_source_id,chatwoot_conversation_id,status,last_verified_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,'ACTIVE',?,?,?)",
            (
                int(binding["id"]), external_id, room_id, CONTACT_ID,
                "ci-source", CONVERSATION_ID, now, now, now,
            ),
        )
        projection_id = int(cur.lastrowid)
        conn.execute(
            "INSERT OR REPLACE INTO room_links"
            "(room_id,contact_id,source_id,conversation_id,created_at) VALUES(?,?,?,?,?)",
            (room_id, CONTACT_ID, "ci-source", CONVERSATION_ID, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO event_deliveries"
            "(event_id,chatwoot_binding_id,direction,status,created_at,delivered_at) "
            "VALUES(?,?,?,'DELIVERED',?,?)",
            ("$ci-already-delivered", int(binding["id"]), "matrix_to_chatwoot", now, now),
        )
    return projection_id


def delivered_sentinel_count(binding_id: int) -> int:
    with legacy.db() as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM event_deliveries "
            "WHERE event_id='$ci-already-delivered' AND chatwoot_binding_id=?",
            (binding_id,),
        ).fetchone()[0])


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), ChatwootDouble)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        legacy.set_setting("chatwoot_base_url", f"http://127.0.0.1:{PORT}")
        legacy.set_setting("chatwoot_account_id", str(ACCOUNT_ID))
        legacy.set_setting("chatwoot_inbox_id", str(INBOX_ID))
        legacy.set_setting("chatwoot_api_token", "journey-profile-token")
        legacy.set_setting("sync_contact_profiles", "1")
        legacy.set_setting("import_history_on_join", "1")
        legacy.set_setting("history_import_days", "30")

        binding = bindings.activate_target(
            bindings.BindingTarget(
                f"http://127.0.0.1:{PORT}",
                ACCOUNT_ID,
                INBOX_ID,
                "ci-profile-reconcile-api",
                "Channel::Api",
            ),
            "ci_live_reconciliation",
            force_new=True,
        )

        admin_token = admin_login()
        mxc = upload_avatar(admin_token)
        encoded_admin = quote(ADMIN_MXID, safe="")
        matrix_put(
            admin_token,
            f"/_matrix/client/v3/profile/{encoded_admin}/displayname",
            {"displayname": "CI Reconciliation Contact"},
        )
        matrix_put(
            admin_token,
            f"/_matrix/client/v3/profile/{encoded_admin}/avatar_url",
            {"avatar_url": mxc},
        )

        projection_id = seed_projection(binding)
        with legacy.db() as conn:
            projection = conn.execute(
                "SELECT * FROM conversation_bindings WHERE id=?", (projection_id,)
            ).fetchone()

        before_dedupe = delivered_sentinel_count(int(binding["id"]))
        if before_dedupe != 1:
            fail(f"sentinel delivery missing before profile reconcile: {before_dedupe}")

        changed = bindings._reconcile_projection_profile(
            binding, projection, ADMIN_MXID, force=True
        )
        if not changed:
            fail("production profile reconciliation did not report a successful update")

        update = wait_until(
            lambda: ChatwootDouble.profile_updates[-1]
            if ChatwootDouble.profile_updates else None,
            "Chatwoot contact profile PUT was not observed",
        )
        if "multipart/form-data" not in update["content_type"]:
            fail(f"avatar update was not multipart: {update['content_type']!r}")
        if AVATAR_BYTES not in update["body"]:
            fail("multipart Chatwoot profile update did not contain Matrix avatar bytes")
        if b"CI Reconciliation Contact" not in update["body"]:
            fail("multipart Chatwoot profile update did not contain Matrix display name")

        with legacy.db() as conn:
            synced = conn.execute(
                "SELECT profile_sync_version,profile_synced_at,profile_sync_error "
                "FROM conversation_bindings WHERE id=?",
                (projection_id,),
            ).fetchone()
        if int(synced["profile_sync_version"]) != bindings.PROFILE_SYNC_VERSION:
            fail(f"wrong profile sync version: {dict(synced)}")
        if int(synced["profile_synced_at"] or 0) <= 0 or str(synced["profile_sync_error"] or ""):
            fail(f"profile reconciliation persistence invalid: {dict(synced)}")
        if delivered_sentinel_count(int(binding["id"])) != 1:
            fail("profile reconciliation mutated generation-scoped message dedupe")

        # Now exercise the production policy-change path. Turn async execution back
        # on only for the explicit request; importing runtime_entrypoint above did
        # not create a second /sync owner.
        os.environ["START_MATRIX_SYNC"] = "true"
        history.save_operations_settings_days(
            auto_join=True,
            import_history=True,
            history_limit=45,
            sync_profiles=True,
            repair_deleted=False,
        )
        if legacy.get_setting("sync_policy_revision") != "1":
            fail(
                "history-days change did not create sync policy revision 1: "
                + legacy.get_setting("sync_policy_revision")
            )
        reason = legacy.get_setting("sync_reconcile_requested_reason")
        if "history_days:30->45" not in reason:
            fail(f"history-days reconcile reason missing: {reason!r}")
        wait_until(
            lambda: legacy.get_setting("sync_reconcile_completed_at"),
            "sync-policy reconciliation did not complete",
        )
        if legacy.get_setting("sync_reconcile_last_error"):
            fail(
                "sync-policy reconciliation recorded an error: "
                + legacy.get_setting("sync_reconcile_last_error")
            )
        if delivered_sentinel_count(int(binding["id"])) != 1:
            fail("sync-policy reconciliation replayed or deleted a delivered event")

        print(
            "PASS: live profile/avatar + sync-policy reconciliation Docker journey",
            flush=True,
        )
    finally:
        os.environ["START_MATRIX_SYNC"] = "false"
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
