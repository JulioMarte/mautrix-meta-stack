import hashlib
import hmac
import os
import sqlite3
import threading
import time
import uuid
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from db_connection_safety import ClosingConnectionProxy
from flask import Flask, abort, redirect, render_template_string, request, session, url_for

DATA_DIR = os.getenv("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "integration.db")
MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", "http://synapse:8008").rstrip("/")
MATRIX_ADMIN_MXID = os.environ["MATRIX_ADMIN_MXID"]
MATRIX_ADMIN_PASSWORD = os.environ["MATRIX_ADMIN_PASSWORD"]
ADMIN_PASSWORD = os.environ["INTEGRATION_ADMIN_PASSWORD"]
WEBHOOK_SECRET = os.environ["CHATWOOT_WEBHOOK_SECRET"]
SESSION_SECRET = os.environ["INTEGRATION_SESSION_SECRET"]
MATRIX_SERVER_NAME = MATRIX_ADMIN_MXID.split(":", 1)[1]
REGISTRATION_PATH = os.getenv("MAUTRIX_REGISTRATION_PATH", "/mautrix/registration.yaml")

os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = SESSION_SECRET
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.getenv("INTEGRATION_COOKIE_SECURE", "true").lower() == "true",
    SESSION_COOKIE_SAMESITE="Strict",
    PERMANENT_SESSION_LIFETIME=8 * 60 * 60,
)


def db():
    """Return a connection that is actually released after context-manager use.

    sqlite3.Connection commits/rolls back on context exit but does not close.
    Most of this integration uses `with db() as conn` and historically assumed
    exit released the handle. Make that contract true at the source so tests,
    helper scripts and production imports are equally safe, even before
    runtime_entrypoint installs its defensive compatibility wrapper.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return ClosingConnectionProxy(conn)


# db_connection_safety.install() is retained as a compatibility guard for alternate
# legacy modules, but this module is natively safe and must not be double-wrapped.
_db_connection_safety_installed = True


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS room_links (
              room_id TEXT PRIMARY KEY,
              contact_id INTEGER NOT NULL,
              source_id TEXT NOT NULL,
              conversation_id INTEGER NOT NULL,
              created_at INTEGER NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS room_links_conversation
              ON room_links(conversation_id);
            CREATE TABLE IF NOT EXISTS processed_events (
              event_id TEXT PRIMARY KEY,
              direction TEXT NOT NULL,
              created_at INTEGER NOT NULL
            );
            """
        )


def get_setting(key, default=""):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def configured():
    return all(
        get_setting(k)
        for k in ("chatwoot_base_url", "chatwoot_account_id", "chatwoot_inbox_id", "chatwoot_api_token")
    )


def csrf_token():
    token = session.get("csrf")
    if not token:
        token = uuid.uuid4().hex
        session["csrf"] = token
    return token


def require_csrf():
    supplied = request.form.get("csrf", "")
    expected = session.get("csrf", "")
    if not supplied or not expected or not hmac.compare_digest(supplied, expected):
        abort(403)


def require_admin():
    if not session.get("admin"):
        return redirect(url_for("login"))
    return None


def redact_proxy(value):
    if not value:
        return "Direct connection (no proxy)"
    try:
        parts = urlsplit(value)
        if parts.username or parts.password:
            host = parts.hostname or ""
            if parts.port:
                host += f":{parts.port}"
            user = quote(parts.username or "user", safe="")
            return urlunsplit((parts.scheme, f"{user}:***@{host}", parts.path, parts.query, parts.fragment))
    except Exception:
        pass
    return value


def bridge_bot_mxid():
    try:
        with open(REGISTRATION_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip().startswith("sender_localpart:"):
                    localpart = line.split(":", 1)[1].strip().strip("'\"")
                    if localpart:
                        return f"@{localpart}:{MATRIX_SERVER_NAME}"
    except OSError:
        pass
    return ""


_matrix_token = None
_matrix_lock = threading.Lock()


def matrix_token():
    global _matrix_token
    with _matrix_lock:
        if _matrix_token:
            return _matrix_token
        localpart = MATRIX_ADMIN_MXID.split(":", 1)[0][1:]
        payload = {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": localpart},
            "password": MATRIX_ADMIN_PASSWORD,
            "initial_device_display_name": "Chatwoot integration",
        }
        resp = requests.post(f"{MATRIX_HOMESERVER}/_matrix/client/v3/login", json=payload, timeout=15)
        resp.raise_for_status()
        _matrix_token = resp.json()["access_token"]
        return _matrix_token


def matrix_headers():
    return {"Authorization": f"Bearer {matrix_token()}"}


def chatwoot_headers():
    return {
        "api_access_token": get_setting("chatwoot_api_token"),
        "Content-Type": "application/json",
    }


def chatwoot_url(path):
    return get_setting("chatwoot_base_url").rstrip("/") + path


def cw_post(path, payload):
    resp = requests.post(chatwoot_url(path), headers=chatwoot_headers(), json=payload, timeout=20)
    resp.raise_for_status()
    return resp.json()


def ensure_room_link(room_id, sender):
    with db() as conn:
        row = conn.execute("SELECT * FROM room_links WHERE room_id = ?", (room_id,)).fetchone()
        if row:
            return row

    account_id = int(get_setting("chatwoot_account_id"))
    inbox_id = int(get_setting("chatwoot_inbox_id"))
    identifier = "matrix-room:" + hashlib.sha256(room_id.encode()).hexdigest()[:32]
    source_id = "mx-" + hashlib.sha256((room_id + ":source").encode()).hexdigest()[:30]
    display_name = sender.split(":", 1)[0].lstrip("@").replace("_", " ") or "Meta contact"

    contact = cw_post(
        f"/api/v1/accounts/{account_id}/contacts",
        {
            "inbox_id": inbox_id,
            "name": display_name,
            "identifier": identifier,
            "additional_attributes": {"matrix_room_id": room_id},
        },
    )
    contact_id = contact.get("id")
    if not contact_id and contact.get("payload"):
        contact_id = contact["payload"][0].get("id")
    if not contact_id:
        raise RuntimeError("Chatwoot did not return a contact id")

    try:
        cw_post(
            f"/api/v1/accounts/{account_id}/contacts/{contact_id}/contact_inboxes",
            {"inbox_id": inbox_id, "source_id": source_id},
        )
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code not in (409, 422):
            raise

    conversation = cw_post(
        f"/api/v1/accounts/{account_id}/conversations",
        {
            "source_id": source_id,
            "inbox_id": inbox_id,
            "contact_id": contact_id,
            "status": "open",
            "custom_attributes": {"matrix_room_id": room_id},
        },
    )
    conversation_id = int(conversation["id"])
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO room_links(room_id, contact_id, source_id, conversation_id, created_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (room_id, int(contact_id), source_id, conversation_id, int(time.time())),
        )
        return conn.execute("SELECT * FROM room_links WHERE room_id = ?", (room_id,)).fetchone()


def event_seen(event_id):
    if not event_id:
        return False
    with db() as conn:
        return conn.execute("SELECT 1 FROM processed_events WHERE event_id = ?", (event_id,)).fetchone() is not None


def mark_event(event_id, direction):
    if not event_id:
        return
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_events(event_id, direction, created_at) VALUES(?, ?, ?)",
            (event_id, direction, int(time.time())),
        )
        conn.execute("DELETE FROM processed_events WHERE created_at < ?", (int(time.time()) - 30 * 86400,))


def matrix_event_to_chatwoot(room_id, event):
    if not configured() or event.get("type") != "m.room.message":
        return
    sender = event.get("sender", "")
    if sender == MATRIX_ADMIN_MXID or sender == bridge_bot_mxid():
        return
    content = event.get("content") or {}
    if content.get("msgtype") not in ("m.text", "m.notice"):
        return
    body = str(content.get("body") or "").strip()
    if not body:
        return
    event_id = event.get("event_id", "")
    if event_seen(event_id):
        return

    link = ensure_room_link(room_id, sender)
    account_id = int(get_setting("chatwoot_account_id"))
    cw_post(
        f"/api/v1/accounts/{account_id}/conversations/{link['conversation_id']}/messages",
        {"content": body, "message_type": "incoming", "private": False, "content_type": "text"},
    )
    mark_event(event_id, "matrix_to_chatwoot")


def sync_once():
    since = get_setting("matrix_next_batch")
    params = {"timeout": 25000}
    if since:
        params["since"] = since
    resp = requests.get(
        f"{MATRIX_HOMESERVER}/_matrix/client/v3/sync",
        headers=matrix_headers(),
        params=params,
        timeout=35,
    )
    resp.raise_for_status()
    data = resp.json()
    next_batch = data.get("next_batch")
    if not since:
        if next_batch:
            set_setting("matrix_next_batch", next_batch)
        return

    joined = (data.get("rooms") or {}).get("join") or {}
    for room_id, room in joined.items():
        for event in ((room.get("timeline") or {}).get("events") or []):
            try:
                matrix_event_to_chatwoot(room_id, event)
            except Exception as exc:
                print(f"matrix event failed room={room_id} event={event.get('event_id')}: {exc}", flush=True)
                return
    if next_batch:
        set_setting("matrix_next_batch", next_batch)


def matrix_sync_loop():
    while True:
        try:
            sync_once()
        except Exception as exc:
            print(f"matrix sync error: {exc}", flush=True)
            global _matrix_token
            if getattr(getattr(exc, "response", None), "status_code", None) == 401:
                _matrix_token = None
            time.sleep(5)


def send_matrix_message(room_id, content, txn_id):
    encoded_room = quote(room_id, safe="")
    encoded_txn = quote(txn_id, safe="")
    resp = requests.put(
        f"{MATRIX_HOMESERVER}/_matrix/client/v3/rooms/{encoded_room}/send/m.room.message/{encoded_txn}",
        headers=matrix_headers(),
        json={"msgtype": "m.text", "body": content},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


BASE_STYLE = """
<style>
body{font-family:system-ui,sans-serif;max-width:980px;margin:40px auto;padding:0 20px;background:#f7f7f8;color:#18181b}
.card{background:white;border:1px solid #ddd;border-radius:12px;padding:20px;margin:16px 0}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
label{display:block;font-weight:600;margin-top:12px}input{box-sizing:border-box;width:100%;padding:10px;border:1px solid #bbb;border-radius:8px;margin-top:5px}
button{padding:10px 16px;border:0;border-radius:8px;background:#18181b;color:white;cursor:pointer}.muted{color:#666;font-size:.92rem}.ok{color:#08783f}.warn{color:#a34c00}
code{background:#eee;padding:2px 5px;border-radius:4px}@media(max-width:700px){.grid{grid-template-columns:1fr}}
</style>
"""


@app.after_request
def security_headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
    return resp


@app.get("/health")
def health():
    return {"ok": True, "configured": configured()}


@app.route("/admin/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            session.clear()
            session["admin"] = True
            session.permanent = True
            csrf_token()
            return redirect(url_for("admin"))
        error = "Invalid password"
    return render_template_string(
        "<!doctype html><title>Integration admin</title>" + BASE_STYLE + """
        <div class=card><h1>Integration admin</h1><p class=muted>Single-client Matrix ↔ Chatwoot configuration.</p>
        {% if error %}<p class=warn>{{error}}</p>{% endif %}
        <form method=post><label>Password</label><input type=password name=password required><p><button>Sign in</button></p></form></div>
        """, error=error)


@app.post("/admin/logout")
def logout():
    gate = require_admin()
    if gate: return gate
    require_csrf()
    session.clear()
    return redirect(url_for("login"))


@app.get("/admin")
def admin():
    gate = require_admin()
    if gate: return gate
    with db() as conn:
        link_count = conn.execute("SELECT COUNT(*) n FROM room_links").fetchone()["n"]
    proxy_enabled = get_setting("proxy_enabled") == "1"
    proxy_value = get_setting("proxy_url")
    return render_template_string(
        "<!doctype html><title>Integration admin</title>" + BASE_STYLE + """
        <h1>Matrix ↔ Chatwoot</h1>
        <div class=card><b>Status:</b> {% if configured %}<span class=ok>Chatwoot configured</span>{% else %}<span class=warn>Needs Chatwoot configuration</span>{% endif %}
        · {{link_count}} linked Matrix rooms<br><span class=muted>Proxy: {{proxy_display}}</span></div>
        <form class=card method=post action=/admin/settings>
          <input type=hidden name=csrf value="{{csrf}}">
          <h2>Chatwoot</h2><div class=grid>
          <div><label>Base URL</label><input name=chatwoot_base_url value="{{base}}" placeholder="https://chatwoot.example.com" required></div>
          <div><label>Account ID</label><input name=chatwoot_account_id value="{{account}}" inputmode=numeric required></div>
          <div><label>Inbox ID</label><input name=chatwoot_inbox_id value="{{inbox}}" inputmode=numeric required></div>
          <div><label>API token</label><input type=password name=chatwoot_api_token placeholder="Leave blank to keep current token"><p class=muted>Stored only in the private integration volume and never rendered back.</p></div></div>
          <h2>Meta proxy</h2><label><input style="width:auto" type=checkbox name=proxy_enabled value=1 {% if proxy_enabled %}checked{% endif %}> Use a proxy for this client instance</label>
          <label>Proxy URL</label><input type=password name=proxy_url placeholder="http://user:password@host:port"><p class=muted>Leave blank to keep the current proxy. When disabled, mautrix-meta receives an empty proxy URL and connects directly.</p>
          <p><button>Save configuration</button></p>
        </form>
        <div class=card><h2>Webhook</h2><p>Configure Chatwoot to POST <code>{{webhook}}</code>.</p><p class=muted>The secret is kept in the deployment environment and is intentionally not shown here.</p>
        <form method=post action=/admin/test-chatwoot><input type=hidden name=csrf value="{{csrf}}"><button>Test Chatwoot connection</button></form></div>
        <form method=post action=/admin/logout><input type=hidden name=csrf value="{{csrf}}"><button>Sign out</button></form>
        """,
        configured=configured(), link_count=link_count, proxy_display=redact_proxy(proxy_value) if proxy_enabled else "Direct connection",
        proxy_enabled=proxy_enabled, base=get_setting("chatwoot_base_url"), account=get_setting("chatwoot_account_id"), inbox=get_setting("chatwoot_inbox_id"),
        csrf=csrf_token(), webhook="/webhooks/chatwoot/<deployment secret>")


@app.post("/admin/settings")
def save_settings():
    gate = require_admin()
    if gate: return gate
    require_csrf()
    base = request.form.get("chatwoot_base_url", "").strip().rstrip("/")
    account = request.form.get("chatwoot_account_id", "").strip()
    inbox = request.form.get("chatwoot_inbox_id", "").strip()
    if not base.startswith(("http://", "https://")) or not account.isdigit() or not inbox.isdigit():
        abort(400)
    set_setting("chatwoot_base_url", base)
    set_setting("chatwoot_account_id", account)
    set_setting("chatwoot_inbox_id", inbox)
    token = request.form.get("chatwoot_api_token", "").strip()
    if token:
        set_setting("chatwoot_api_token", token)
    set_setting("proxy_enabled", "1" if request.form.get("proxy_enabled") == "1" else "0")
    proxy = request.form.get("proxy_url", "").strip()
    if proxy:
        parsed = urlsplit(proxy)
        if parsed.scheme not in ("http", "https", "socks5", "socks5h") or not parsed.hostname:
            abort(400)
        set_setting("proxy_url", proxy)
    return redirect(url_for("admin"))


@app.post("/admin/test-chatwoot")
def test_chatwoot():
    gate = require_admin()
    if gate: return gate
    require_csrf()
    if not configured():
        return "Chatwoot is not configured", 400
    account_id = int(get_setting("chatwoot_account_id"))
    resp = requests.get(chatwoot_url(f"/api/v1/accounts/{account_id}/inboxes"), headers=chatwoot_headers(), timeout=15)
    if resp.ok:
        return redirect(url_for("admin"))
    return f"Chatwoot test failed with HTTP {resp.status_code}", 502


@app.get("/internal/proxy")
def proxy_resolver():
    if get_setting("proxy_enabled") != "1":
        return {"proxy_url": ""}
    proxy = get_setting("proxy_url")
    if not proxy:
        return {"error": "proxy enabled but not configured"}, 503
    return {"proxy_url": proxy}


@app.post("/webhooks/chatwoot/<secret>")
def chatwoot_webhook(secret):
    if not hmac.compare_digest(secret, WEBHOOK_SECRET):
        abort(404)
    payload = request.get_json(silent=True) or {}
    if payload.get("event") != "message_created":
        return {"ok": True, "ignored": True}
    message_type = payload.get("message_type")
    if message_type not in ("outgoing", 1) or payload.get("private") is True:
        return {"ok": True, "ignored": True}
    conversation = payload.get("conversation") or {}
    conversation_id = conversation.get("id") or payload.get("conversation_id")
    content = str(payload.get("content") or "").strip()
    message_id = str(payload.get("id") or "")
    if not conversation_id or not content:
        return {"ok": True, "ignored": True}
    event_key = "chatwoot:" + message_id if message_id else ""
    if event_key and event_seen(event_key):
        return {"ok": True, "duplicate": True}
    with db() as conn:
        link = conn.execute("SELECT * FROM room_links WHERE conversation_id = ?", (int(conversation_id),)).fetchone()
    if not link:
        return {"ok": True, "ignored": True, "reason": "unmapped conversation"}
    send_matrix_message(link["room_id"], content, "cw-" + (message_id or uuid.uuid4().hex))
    if event_key:
        mark_event(event_key, "chatwoot_to_matrix")
    return {"ok": True}


if __name__ == "__main__":
    init_db()
    threading.Thread(target=matrix_sync_loop, name="matrix-sync", daemon=True).start()
    app.run(host="0.0.0.0", port=8080, threaded=True)
