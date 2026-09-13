import base64
import json
import os
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8080"
PROXY_SECRET = os.environ["META_PROXY_RESOLVER_SECRET"]
WEBHOOK_SECRET = os.environ["CHATWOOT_WEBHOOK_SECRET"]


def get(path, headers=None):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    return urllib.request.urlopen(req, timeout=10)


def post(path, body=b"", headers=None):
    req = urllib.request.Request(BASE + path, data=body, method="POST", headers=headers or {})
    return urllib.request.urlopen(req, timeout=10)


def expect_http_error(code, fn):
    try:
        fn()
    except urllib.error.HTTPError as exc:
        assert exc.code == code, (exc.code, code)
        return
    raise AssertionError(f"expected HTTP {code}")


health = get("/health")
health_body = health.read().decode()
assert health.status == 200
assert '"admin_ui":"nicegui"' in health_body.replace(" ", "")
assert PROXY_SECRET not in health_body
assert WEBHOOK_SECRET not in health_body
assert health.headers.get("x-content-type-options") == "nosniff"
assert health.headers.get("referrer-policy") == "no-referrer"
assert health.headers.get("x-frame-options") == "DENY"

root = get("/")
assert root.status == 200  # urllib follows the 303 to /admin and then NiceGUI login navigation shell
root_body = root.read().decode()
assert "nicegui" in root_body.lower()
assert PROXY_SECRET not in root_body
assert WEBHOOK_SECRET not in root_body

login = get("/admin/login")
login_body = login.read().decode()
assert login.status == 200
assert "text/html" in login.headers.get("content-type", "")
assert "nicegui" in login_body.lower()
assert login.headers.get("cache-control") == "no-store"
assert PROXY_SECRET not in login_body
assert WEBHOOK_SECRET not in login_body

expect_http_error(404, lambda: get("/internal/proxy"))
expect_http_error(404, lambda: get("/internal/proxy", {"Authorization": "Basic not-base64"}))
wrong = base64.b64encode(b"mautrix:wrong-secret-long-value").decode()
expect_http_error(404, lambda: get("/internal/proxy", {"Authorization": "Basic " + wrong}))

basic = base64.b64encode(("mautrix:" + PROXY_SECRET).encode()).decode()
resolver = get("/internal/proxy", {"Authorization": "Basic " + basic})
resolver_body = resolver.read().decode()
assert resolver.status == 200
assert json.loads(resolver_body) == {"proxy_url": ""}

expect_http_error(404, lambda: get("/internal/proxy/" + PROXY_SECRET))
expect_http_error(404, lambda: post("/webhooks/chatwoot/wrong-secret", b"{}", {"Content-Type": "application/json"}))
expect_http_error(
    400,
    lambda: post(
        "/webhooks/chatwoot/" + WEBHOOK_SECRET,
        b"not-json",
        {"Content-Type": "application/json"},
    ),
)

print("NiceGUI admin/runtime HTTP smoke test passed")
