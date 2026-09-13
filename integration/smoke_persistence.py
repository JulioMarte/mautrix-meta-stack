import http.cookiejar
import os
import re
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8080"
PASSWORD = os.environ["INTEGRATION_ADMIN_PASSWORD"]

cookies = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))

login_page = opener.open(BASE + "/admin/login", timeout=10)
login_html = login_page.read().decode()
match = re.search(r'name=csrf value="([^"]+)"', login_html)
assert match, "login CSRF token missing after restart"

body = urllib.parse.urlencode({"csrf": match.group(1), "password": PASSWORD}).encode()
response = opener.open(
    urllib.request.Request(BASE + "/admin/login", data=body, method="POST"),
    timeout=10,
)
html = response.read().decode()

assert response.status == 200
assert "Matrix ↔ Chatwoot" in html
assert "https://chatwoot.example.com" in html
assert "Chatwoot configured" in html
assert "smoke-chatwoot-token-must-not-render" not in html
print("admin persistence smoke test passed")
