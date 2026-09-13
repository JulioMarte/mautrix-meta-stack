"""Production hardening layer for the single-client integration service."""
import hmac
import os
import threading
import time
from urllib.parse import quote, urlsplit

import requests
from flask import abort, redirect, render_template_string, request, session, url_for

import app as legacy

application = legacy.app

PROXY_RESOLVER_SECRET = os.environ["META_PROXY_RESOLVER_SECRET"]
ENV_PROXY_URL = os.getenv("META_PROXY_URL", "").strip()
ENV_PROXY_ENABLED = os.getenv("META_PROXY_ENABLED", "true" if ENV_PROXY_URL else "false").lower() == "true"
ALLOW_INSECURE_CHATWOOT = os.getenv("ALLOW_INSECURE_CHATWOOT", "false").lower() == "true"
START_MATRIX_SYNC = os.getenv("START_MATRIX_SYNC", "true").lower() == "true"
COOKIE_SECURE = os.getenv("INTEGRATION_COOKIE_SECURE", "true").lower() == "true"


def validate_proxy_url(value):
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https", "socks5", "socks5h") or not parsed.hostname or not parsed.port:
        raise ValueError("Proxy URL must include scheme, host and port")
    return value


def validate_runtime():
    for name, value in {
        "INTEGRATION_ADMIN_PASSWORD": legacy.ADMIN_PASSWORD,
        "INTEGRATION_SESSION_SECRET": legacy.SESSION_SECRET,
        "CHATWOOT_WEBHOOK_SECRET": legacy.WEBHOOK_SECRET,
        "META_PROXY_RESOLVER_SECRET": PROXY_RESOLVER_SECRET,
    }.items():
        if len(value) < 16:
            raise RuntimeError(f"{name} must be at least 16 characters")
    if ENV_PROXY_URL:
        validate_proxy_url(ENV_PROXY_URL)


def effective_proxy():
    if ENV_PROXY_URL:
        return True, ENV_PROXY_ENABLED, ENV_PROXY_URL
    return False, legacy.get_setting("proxy_enabled") == "1", legacy.get_setting("proxy_url")


def cw_get(path, params=None):
    resp = requests.get(legacy.chatwoot_url(path), headers=legacy.chatwoot_headers(), params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def contact_object(payload):
    if isinstance(payload, dict) and payload.get("id"):
        return payload
    if isinstance(payload, dict):
        items = payload.get("payload") or []
        if items and isinstance(items[0], dict):
            return items[0]
    return None


def contact_source_id(contact, inbox_id):
    for item in (contact or {}).get("contact_inboxes") or []:
        item_inbox = item.get("inbox_id")
        if item_inbox is None and isinstance(item.get("inbox"), dict):
            item_inbox = item["inbox"].get("id")
        try:
            same_inbox = int(item_inbox) == int(inbox_id)
        except (TypeError, ValueError):
            same_inbox = False
        if same_inbox and item.get("source_id"):
            return str(item["source_id"])
    return ""


def find_contact_by_identifier(account_id, identifier):
    data = cw_get(f"/api/v1/accounts/{account_id}/contacts/search", {"q": identifier})
    for item in data.get("payload") or []:
        if item.get("identifier") == identifier:
            return item
    return None


def ensure_room_link(room_id, sender):
    with legacy.db() as conn:
        row = conn.execute("SELECT * FROM room_links WHERE room_id = ?", (room_id,)).fetchone()
        if row:
            return row

    account_id = int(legacy.get_setting("chatwoot_account_id"))
    inbox_id = int(legacy.get_setting("chatwoot_inbox_id"))
    identifier = "matrix-room:" + legacy.hashlib.sha256(room_id.encode()).hexdigest()[:32]
    generated_source_id = "mx-" + legacy.hashlib.sha256((room_id + ":source").encode()).hexdigest()[:30]
    display_name = sender.split(":", 1)[0].lstrip("@").replace("_", " ") or "Meta contact"
    try:
        raw = legacy.cw_post(
            f"/api/v1/accounts/{account_id}/contacts",
            {"inbox_id": inbox_id, "name": display_name, "identifier": identifier,
             "additional_attributes": {"matrix_room_id": room_id}},
        )
        contact = contact_object(raw)
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code not in (409, 422):
            raise
        contact = find_contact_by_identifier(account_id, identifier)
    if not contact or not contact.get("id"):
        raise RuntimeError("Chatwoot contact could not be created or resolved")
    contact_id = int(contact["id"])
    source_id = contact_source_id(contact, inbox_id)
    if not source_id:
        assoc = legacy.cw_post(
            f"/api/v1/accounts/{account_id}/contacts/{contact_id}/contact_inboxes",
            {"inbox_id": inbox_id, "source_id": generated_source_id},
        )
        source_id = str((assoc or {}).get("source_id") or generated_source_id)
    conversation = legacy.cw_post(
        f"/api/v1/accounts/{account_id}/conversations",
        {"source_id": source_id, "inbox_id": inbox_id, "contact_id": contact_id, "status": "open",
         "custom_attributes": {"matrix_room_id": room_id}},
    )
    conversation_id = int(conversation["id"])
    with legacy.db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO room_links(room_id, contact_id, source_id, conversation_id, created_at) VALUES(?, ?, ?, ?, ?)",
            (room_id, contact_id, source_id, conversation_id, int(time.time())),
        )
        return conn.execute("SELECT * FROM room_links WHERE room_id = ?", (room_id,)).fetchone()


legacy.ensure_room_link = ensure_room_link

_portal_cache = set()


def is_bridge_portal(room_id):
    if room_id in _portal_cache:
        return True
    encoded = quote(room_id, safe="")
    resp = requests.get(
        f"{legacy.MATRIX_HOMESERVER}/_matrix/client/v3/rooms/{encoded}/state",
        headers=legacy.matrix_headers(), timeout=15,
    )
    if resp.status_code in (403, 404):
        return False
    resp.raise_for_status()
    expected_bot = legacy.bridge_bot_mxid()
    for event in resp.json():
        if event.get("type") not in ("m.bridge", "uk.half-shot.bridge"):
            continue
        bridgebot = (event.get("content") or {}).get("bridgebot") or ""
        if expected_bot and bridgebot and bridgebot != expected_bot:
            continue
        _portal_cache.add(room_id)
        return True
    return False


def matrix_event_to_chatwoot(room_id, event):
    if not legacy.configured() or event.get("type") != "m.room.message":
        return
    sender = event.get("sender", "")
    if sender in (legacy.MATRIX_ADMIN_MXID, legacy.bridge_bot_mxid()):
        return
    content = event.get("content") or {}
    body = str(content.get("body") or "").strip()
    event_id = event.get("event_id", "")
    if content.get("msgtype") not in ("m.text", "m.notice") or not body or legacy.event_seen(event_id):
        return
    if not is_bridge_portal(room_id):
        return
    link = ensure_room_link(room_id, sender)
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    legacy.cw_post(
        f"/api/v1/accounts/{account_id}/conversations/{link['conversation_id']}/messages",
        {"content": body, "message_type": "incoming", "private": False, "content_type": "text"},
    )
    legacy.mark_event(event_id, "matrix_to_chatwoot")


legacy.matrix_event_to_chatwoot = matrix_event_to_chatwoot


def login():
    error = ""
    if request.method == "POST":
        legacy.require_csrf()
        if hmac.compare_digest(request.form.get("password", ""), legacy.ADMIN_PASSWORD):
            session.clear()
            session["admin"] = True
            session.permanent = True
            legacy.csrf_token()
            return redirect(url_for("admin"))
        time.sleep(0.35)
        error = "Invalid password"
    return render_template_string(
        "<!doctype html><title>Integration admin</title>" + legacy.BASE_STYLE + """
        <div class=card><h1>Integration admin</h1><p class=muted>Single-client Matrix ↔ Chatwoot configuration.</p>
        {% if error %}<p class=warn>{{error}}</p>{% endif %}
        <form method=post><input type=hidden name=csrf value="{{csrf}}"><label>Password</label><input type=password name=password required autocomplete=current-password><p><button>Sign in</button></p></form></div>
        """, error=error, csrf=legacy.csrf_token())


def admin():
    gate = legacy.require_admin()
    if gate: return gate
    with legacy.db() as conn:
        link_count = conn.execute("SELECT COUNT(*) n FROM room_links").fetchone()["n"]
    managed, enabled, proxy = effective_proxy()
    return render_template_string(
        "<!doctype html><title>Integration admin</title>" + legacy.BASE_STYLE + """
        <h1>Matrix ↔ Chatwoot</h1>{% if result %}<div class=card><span class=ok>{{result}}</span></div>{% endif %}
        <div class=card><b>Status:</b> {% if configured %}<span class=ok>Chatwoot configured</span>{% else %}<span class=warn>Needs Chatwoot configuration</span>{% endif %} · {{links}} linked rooms<br>
        <span class=muted>Proxy: {{proxy_display}}{% if managed %} · managed by Coolify{% endif %}</span></div>
        <form class=card method=post action=/admin/settings><input type=hidden name=csrf value="{{csrf}}"><h2>Chatwoot</h2><div class=grid>
        <div><label>Base URL</label><input name=chatwoot_base_url value="{{base}}" placeholder="https://chatwoot.example.com" required></div>
        <div><label>Account ID</label><input name=chatwoot_account_id value="{{account}}" inputmode=numeric required></div>
        <div><label>Inbox ID</label><input name=chatwoot_inbox_id value="{{inbox}}" inputmode=numeric required></div>
        <div><label>API token</label><input type=password name=chatwoot_api_token placeholder="Leave blank to keep current token"><p class=muted>Never rendered back.</p></div></div>
        <h2>Meta proxy</h2>{% if managed %}<p class=muted>Managed by META_PROXY_URL / META_PROXY_ENABLED in Coolify; credentials are not editable here.</p>{% else %}
        <label><input style="width:auto" type=checkbox name=proxy_enabled value=1 {% if enabled %}checked{% endif %}> Use proxy</label><label>Proxy URL</label><input type=password name=proxy_url placeholder="http://user:password@host:port">{% endif %}
        <p><button>Save configuration</button></p></form>
        <div class=card><h2>Connectivity tests</h2><form method=post action=/admin/test-chatwoot><input type=hidden name=csrf value="{{csrf}}"><button>Test Chatwoot</button></form>
        {% if enabled %}<form method=post action=/admin/test-proxy style="margin-top:10px"><input type=hidden name=csrf value="{{csrf}}"><button>Test proxy egress</button></form>{% endif %}</div>
        <div class=card><h2>Webhook</h2><p>Configure Chatwoot to POST <code>/webhooks/chatwoot/&lt;CHATWOOT_WEBHOOK_SECRET&gt;</code>.</p></div>
        <form method=post action=/admin/logout><input type=hidden name=csrf value="{{csrf}}"><button>Sign out</button></form>
        """, configured=legacy.configured(), links=link_count, managed=managed, enabled=enabled,
        proxy_display=legacy.redact_proxy(proxy) if enabled else "Direct connection", csrf=legacy.csrf_token(),
        base=legacy.get_setting("chatwoot_base_url"), account=legacy.get_setting("chatwoot_account_id"),
        inbox=legacy.get_setting("chatwoot_inbox_id"), result=request.args.get("result", ""))


def save_settings():
    gate = legacy.require_admin()
    if gate: return gate
    legacy.require_csrf()
    base = request.form.get("chatwoot_base_url", "").strip().rstrip("/")
    parsed = urlsplit(base)
    scheme_ok = parsed.scheme == "https" or (ALLOW_INSECURE_CHATWOOT and parsed.scheme == "http")
    account = request.form.get("chatwoot_account_id", "").strip()
    inbox = request.form.get("chatwoot_inbox_id", "").strip()
    if not scheme_ok or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        abort(400)
    if not account.isdigit() or not inbox.isdigit():
        abort(400)
    legacy.set_setting("chatwoot_base_url", base)
    legacy.set_setting("chatwoot_account_id", account)
    legacy.set_setting("chatwoot_inbox_id", inbox)
    token = request.form.get("chatwoot_api_token", "").strip()
    if token:
        legacy.set_setting("chatwoot_api_token", token)
    managed, _, _ = effective_proxy()
    if not managed:
        legacy.set_setting("proxy_enabled", "1" if request.form.get("proxy_enabled") == "1" else "0")
        proxy = request.form.get("proxy_url", "").strip()
        if proxy:
            try:
                validate_proxy_url(proxy)
            except ValueError:
                abort(400)
            legacy.set_setting("proxy_url", proxy)
    return redirect(url_for("admin"))


def test_chatwoot():
    gate = legacy.require_admin()
    if gate: return gate
    legacy.require_csrf()
    if not legacy.configured():
        return "Chatwoot is not configured", 400
    account_id = int(legacy.get_setting("chatwoot_account_id"))
    inbox_id = str(legacy.get_setting("chatwoot_inbox_id"))
    data = cw_get(f"/api/v1/accounts/{account_id}/inboxes")
    inboxes = data.get("payload") if isinstance(data, dict) else data
    if not isinstance(inboxes, list) or not any(str(item.get("id")) == inbox_id for item in inboxes):
        return "Configured Chatwoot inbox was not found", 502
    return redirect(url_for("admin", result="Chatwoot connection verified"))


def test_proxy():
    gate = legacy.require_admin()
    if gate: return gate
    legacy.require_csrf()
    _, enabled, proxy = effective_proxy()
    if not enabled or not proxy:
        return "Proxy is not configured", 400
    try:
        resp = requests.get("https://api.ipify.org?format=json", proxies={"http": proxy, "https": proxy}, timeout=20)
        resp.raise_for_status()
        observed = resp.json().get("ip", "unknown")
    except requests.RequestException:
        return "Proxy test failed", 502
    return redirect(url_for("admin", result=f"Proxy egress verified: {observed}"))


application.view_functions["login"] = login
application.view_functions["admin"] = admin
application.view_functions["save_settings"] = save_settings
application.view_functions["test_chatwoot"] = test_chatwoot
application.view_functions["proxy_resolver"] = lambda: abort(404)

@application.get("/internal/proxy/<secret>")
def protected_proxy_resolver(secret):
    if not hmac.compare_digest(secret, PROXY_RESOLVER_SECRET):
        abort(404)
    _, enabled, proxy = effective_proxy()
    if not enabled:
        return {"proxy_url": ""}
    if not proxy:
        return {"error": "proxy enabled but not configured"}, 503
    return {"proxy_url": proxy}

@application.post("/admin/test-proxy")
def admin_test_proxy():
    return test_proxy()

@application.after_request
def production_headers(resp):
    if COOKIE_SECURE:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp


validate_runtime()
legacy.init_db()
if START_MATRIX_SYNC:
    threading.Thread(target=legacy.matrix_sync_loop, name="matrix-sync", daemon=True).start()
