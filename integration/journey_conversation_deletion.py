#!/usr/bin/env python3
"""Docker journey for destructive conversation deletion.

Runs inside the live integration container while the real Synapse and integration
runtime are running. Chatwoot's external DELETE API is represented by a tiny local
HTTP double; Matrix, /sync delivery, HMAC middleware, SQLite state, and the runtime
process are all real.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import requests
import yaml

import final_app as runtime
import conversation_lifecycle_v10 as lifecycle

legacy = runtime.legacy

MATRIX = os.environ.get("MATRIX_HOMESERVER", "http://synapse:8008").rstrip("/")
ADMIN_MXID = os.environ["MATRIX_ADMIN_MXID"]
ADMIN_PASSWORD = os.environ["MATRIX_ADMIN_PASSWORD"]
REGISTRATION = os.environ.get("MAUTRIX_REGISTRATION_PATH", "/mautrix/registration.yaml")
CALLBACK_SECRET = "journey-delete-secret"
ACCOUNT_ID = 701
INBOX_ID = 702


def fail(message: str) -> None:
    raise AssertionError(message)


def wait_until(predicate, message: str, timeout: float = 25.0, interval: float = 0.25):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except Exception as exc:  # noqa: BLE001 - retain last error for assertion context
            last = exc
        time.sleep(interval)
    fail(f"timeout: {message}; last={last!r}")


def matrix_request(method: str, path: str, token: str, *, user_id: str | None = None, payload=None):
    params = {"user_id": user_id} if user_id else None
    response = requests.request(
        method,
        MATRIX + path,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        params=params,
        json=payload,
        timeout=10,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Matrix {method} {path} -> {response.status_code}: {response.text[:500]}")
    return response.json() if response.content else {}


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


def appservice_identity() -> tuple[str, str]:
    with open(REGISTRATION, "r", encoding="utf-8") as handle:
        registration = yaml.safe_load(handle) or {}
    token = str(registration.get("as_token") or "")
    localpart = str(registration.get("sender_localpart") or "")
    if not token or not localpart:
        fail("mautrix appservice registration has no as_token/sender_localpart")
    bot = f"@{localpart}:{os.environ['MATRIX_SERVER_NAME']}"
    expected = legacy.bridge_bot_mxid()
    if expected != bot:
        fail(f"bridge bot mismatch registration={bot} runtime={expected}")
    return token, bot


def create_portal_room(admin_token: str, as_token: str, bot: str) -> str:
    data = matrix_request(
        "POST",
        "/_matrix/client/v3/createRoom",
        as_token,
        user_id=bot,
        payload={
            "preset": "private_chat",
            "invite": [ADMIN_MXID],
            "name": f"deletion-journey-{int(time.time() * 1000)}",
        },
    )
    room_id = str(data.get("room_id") or "")
    if not room_id:
        fail("bot createRoom did not return room_id")
    matrix_request("POST", f"/_matrix/client/v3/join/{quote(room_id, safe='')}", admin_token, payload={})
    return room_id


def bridge_kick_admin(room_id: str, as_token: str, bot: str) -> None:
    matrix_request(
        "PUT",
        f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/state/m.room.member/{quote(ADMIN_MXID, safe='')}",
        as_token,
        user_id=bot,
        payload={"membership": "leave", "reason": "CI bridge delete confirmation"},
    )


def seed_link(room_id: str, conversation_id: int) -> None:
    now = int(time.time())
    lifecycle.ensure_schema()
    with legacy.db() as conn:
        conn.execute("DELETE FROM conversation_deletions WHERE conversation_id=?", (conversation_id,))
        conn.execute("DELETE FROM room_links WHERE conversation_id=? OR room_id=?", (conversation_id, room_id))
        conn.execute("DELETE FROM verified_meta_portals WHERE room_id=?", (room_id,))
        conn.execute(
            "INSERT INTO room_links(room_id, contact_id, source_id, conversation_id, created_at) VALUES(?,?,?,?,?)",
            (room_id, conversation_id + 10000, f"journey-{conversation_id}", conversation_id, now),
        )
        conn.execute(
            "INSERT INTO verified_meta_portals(room_id, verified_at, last_seen_at) VALUES(?,?,?)",
            (room_id, now, now),
        )


def operation(conversation_id: int):
    with legacy.db() as conn:
        return conn.execute(
            "SELECT * FROM conversation_deletions WHERE conversation_id=?", (conversation_id,)
        ).fetchone()


def link_exists(conversation_id: int) -> bool:
    with legacy.db() as conn:
        return conn.execute(
            "SELECT 1 FROM room_links WHERE conversation_id=?", (conversation_id,)
        ).fetchone() is not None


def signed_callback(payload: dict, *, valid: bool = True):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    signature = "sha256=" + hmac.new(
        CALLBACK_SECRET.encode(), timestamp.encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    if not valid:
        signature = "sha256=" + "0" * 64
    return requests.post(
        "http://127.0.0.1:8080/webhooks/chatwoot/inbox",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "X-Chatwoot-Timestamp": timestamp,
            "X-Chatwoot-Signature": signature,
        },
        timeout=10,
    )


def room_delete_event(admin_token: str, room_id: str):
    encoded = quote(room_id, safe="")
    data = matrix_request(
        "GET",
        f"/_matrix/client/v3/rooms/{encoded}/messages?dir=b&limit=30",
        admin_token,
    )
    for event in data.get("chunk") or []:
        if event.get("type") == lifecycle.DELETE_EVENT_TYPE:
            return event
    return None


class DeleteRecorder(BaseHTTPRequestHandler):
    requests_seen: "queue.Queue[str]" = queue.Queue()
    response_code = 204

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler contract
        expected = f"/api/v1/accounts/{ACCOUNT_ID}/inboxes/{INBOX_ID}"
        if self.path != expected:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({
            "id": INBOX_ID,
            "name": "CI API inbox",
            "channel_type": "Channel::Api",
            "identifier": "ci-delete-journey-api",
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self):  # noqa: N802 - BaseHTTPRequestHandler contract
        self.__class__.requests_seen.put(self.path)
        self.send_response(self.__class__.response_code)
        self.end_headers()

    def log_message(self, fmt, *args):
        print("mock-chatwoot: " + (fmt % args), flush=True)


def configure_integration() -> None:
    legacy.set_setting("chatwoot_base_url", "http://127.0.0.1:8091")
    legacy.set_setting("chatwoot_account_id", str(ACCOUNT_ID))
    legacy.set_setting("chatwoot_inbox_id", str(INBOX_ID))
    legacy.set_setting("chatwoot_api_token", "journey-token")
    legacy.set_setting("chatwoot_api_inbox_signing_secret", CALLBACK_SECRET)


def run_chatwoot_to_meta(admin_token: str, as_token: str, bot: str, conversation_id: int) -> None:
    room_id = create_portal_room(admin_token, as_token, bot)
    seed_link(room_id, conversation_id)
    payload = {
        "event": "conversation_deleted",
        "id": conversation_id,
        "conversation_id": conversation_id,
        "account": {"id": ACCOUNT_ID},
        "inbox": {"id": INBOX_ID},
    }

    rejected = signed_callback(payload, valid=False)
    if rejected.status_code != 502:
        fail(f"invalid destructive HMAC was not rejected: {rejected.status_code} {rejected.text}")
    if operation(conversation_id) is not None:
        fail("invalid HMAC created a destructive operation")

    accepted = signed_callback(payload, valid=True)
    if accepted.status_code != 200:
        fail(f"valid destructive callback failed: {accepted.status_code} {accepted.text}")

    op = wait_until(
        lambda: operation(conversation_id),
        "Chatwoot delete operation was not persisted",
    )
    if op["origin"] != "chatwoot" or op["state"] != "remote_requested" or not op["matrix_event_id"]:
        fail(f"unexpected Chatwoot deletion state: {dict(op)}")
    if not link_exists(conversation_id):
        fail("room link was removed before bridge confirmation")

    event = wait_until(
        lambda: room_delete_event(admin_token, room_id),
        "com.beeper.delete_chat was not written to real Synapse",
    )
    expected_content = {"delete_for_everyone": False, "from_message_request": False}
    if event.get("content") != expected_content:
        fail(f"wrong Matrix delete content: {event.get('content')!r}")

    bridge_kick_admin(room_id, as_token, bot)
    completed = wait_until(
        lambda: (row if (row := operation(conversation_id)) and row["state"] == "completed" else None),
        "trusted bridge leave did not complete Chatwoot-origin deletion",
    )
    if completed["origin"] != "chatwoot":
        fail(f"origin changed during confirmation: {dict(completed)}")
    if link_exists(conversation_id):
        fail("room link survived trusted bridge confirmation")


def run_meta_to_chatwoot(admin_token: str, as_token: str, bot: str, conversation_id: int) -> None:
    cursor_before = legacy.get_setting("matrix_next_batch")
    room_id = create_portal_room(admin_token, as_token, bot)
    seed_link(room_id, conversation_id)

    # Ensure the production /sync owner has observed activity after this room was
    # created before generating the terminal leave. Without this barrier a fast CI
    # runner can create+join+leave between two /sync responses and Synapse may only
    # expose the terminal membership transition, making this journey racy rather
    # than testing the lifecycle contract.
    wait_until(
        lambda: (
            current
            if (current := legacy.get_setting("matrix_next_batch"))
            and current != cursor_before
            else None
        ),
        "integration Matrix sync cursor did not advance for Meta-origin room",
        timeout=40.0,
    )
    bridge_kick_admin(room_id, as_token, bot)

    path = wait_until(
        lambda: DeleteRecorder.requests_seen.get_nowait() if not DeleteRecorder.requests_seen.empty() else None,
        "trusted Meta leave did not call Chatwoot DELETE",
    )
    expected_path = f"/api/v1/accounts/{ACCOUNT_ID}/conversations/{conversation_id}"
    if path != expected_path:
        fail(f"wrong Chatwoot DELETE path: {path!r}")

    completed = wait_until(
        lambda: (row if (row := operation(conversation_id)) and row["state"] == "completed" else None),
        "Meta-origin deletion did not reach completed",
    )
    if completed["origin"] != "meta":
        fail(f"wrong Meta-origin operation: {dict(completed)}")
    if link_exists(conversation_id):
        fail("Meta-origin completed deletion retained room link")


def main() -> None:
    configure_integration()
    admin_token = admin_login()
    as_token, bot = appservice_identity()

    # The lifecycle intentionally treats the first /sync response as cursor
    # initialization. Do not create destructive test rooms until the live runtime
    # has established that cursor, otherwise a fast CI runner could generate the
    # leave inside the initialization window and create a false negative.
    wait_until(
        lambda: legacy.get_setting("matrix_next_batch"),
        "integration Matrix sync cursor was not initialized",
        timeout=40.0,
    )

    server = ThreadingHTTPServer(("127.0.0.1", 8091), DeleteRecorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        suffix = int(time.time()) % 100000
        run_chatwoot_to_meta(admin_token, as_token, bot, 800000 + suffix)
        run_meta_to_chatwoot(admin_token, as_token, bot, 900000 + suffix)
    finally:
        server.shutdown()
        server.server_close()

    print("PASS: bidirectional conversation deletion Docker journey", flush=True)


if __name__ == "__main__":
    main()
