import base64
import http.cookiejar
import os
import re
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8080"
PASSWORD = os.environ["INTEGRATION_ADMIN_PASSWORD"]
PROXY_SECRET = os.environ["META_PROXY_RESOLVER_SECRET"]

cookies = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))


def get(path, headers=None):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    return opener.open(req, timeout=10)


def post(path, data):
    body = urllib.parse.urlencode(data).encode()
    return opener.open(urllib.request.Request(BASE + path, data=body, method="POST"), timeout=10)


def csrf(html):
    match = re.search(r'name=csrf value="([^"]+)"', html)
    if not match:
        raise AssertionError("CSRF token not found")
    return match.group(1)


login_page = get("/admin/login")
login_html = login_page.read().decode()
assert login_page.status == 200
assert "Integration admin" in login_html

login_response = post("/admin/login", {"csrf": csrf(login_html), "password": PASSWORD})
admin_html = login_response.read().decode()
assert login_response.status == 200
assert "Matrix ↔ Chatwoot" in admin_html
assert "Save configuration" in admin_html
assert "Test Chatwoot" in admin_html

admin_csrf = csrf(admin_html)
save_response = post(
    "/admin/settings",
    {
        "csrf": admin_csrf,
        "chatwoot_base_url": "https://chatwoot.example.com",
        "chatwoot_account_id": "1",
        "chatwoot_inbox_id": "2",
        "chatwoot_api_token": "smoke-chatwoot-token-must-not-render",
    },
)
saved_html = save_response.read().decode()
assert save_response.status == 200
assert "https://chatwoot.example.com" in saved_html
assert "smoke-chatwoot-token-must-not-render" not in saved_html

# Resolver is not available without internal Basic Auth.
try:
    get("/internal/proxy")
    raise AssertionError("proxy resolver must reject unauthenticated access")
except urllib.error.HTTPError as exc:
    assert exc.code == 404

# Transitional secret-in-path endpoint is disabled in the final runtime.
try:
    get("/internal/proxy/" + urllib.parse.quote(PROXY_SECRET, safe=""))
    raise AssertionError("secret-in-path proxy resolver must be disabled")
except urllib.error.HTTPError as exc:
    assert exc.code == 404

basic = base64.b64encode(("mautrix:" + PROXY_SECRET).encode()).decode()
resolver = get("/internal/proxy", {"Authorization": "Basic " + basic})
resolver_body = resolver.read().decode()
assert resolver.status == 200
assert '"proxy_url":""' in resolver_body.replace(" ", "")

print("admin smoke test passed")
