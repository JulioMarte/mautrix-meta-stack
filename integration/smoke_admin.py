import base64
import os
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8080"
PROXY_SECRET = os.environ["META_PROXY_RESOLVER_SECRET"]


def get(path, headers=None):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    return urllib.request.urlopen(req, timeout=10)


health = get("/health")
health_body = health.read().decode()
assert health.status == 200
assert '"admin_ui":"nicegui"' in health_body.replace(" ", "")

login = get("/admin/login")
login_body = login.read().decode()
assert login.status == 200
assert "text/html" in login.headers.get("content-type", "")
assert "nicegui" in login_body.lower()

try:
    get("/internal/proxy")
    raise AssertionError("proxy resolver must reject unauthenticated access")
except urllib.error.HTTPError as exc:
    assert exc.code == 404

basic = base64.b64encode(("mautrix:" + PROXY_SECRET).encode()).decode()
resolver = get("/internal/proxy", {"Authorization": "Basic " + basic})
resolver_body = resolver.read().decode()
assert resolver.status == 200
assert '"proxy_url":""' in resolver_body.replace(" ", "")

try:
    get("/internal/proxy/" + PROXY_SECRET)
    raise AssertionError("secret-in-path resolver must stay disabled")
except urllib.error.HTTPError as exc:
    assert exc.code == 404

print("NiceGUI admin HTTP smoke test passed")
