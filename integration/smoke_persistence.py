import urllib.request

BASE = "http://127.0.0.1:8080"

health = urllib.request.urlopen(BASE + "/health", timeout=10)
assert health.status == 200

login = urllib.request.urlopen(BASE + "/admin/login", timeout=10)
assert login.status == 200

import app as legacy

assert legacy.get_setting("chatwoot_base_url") == "https://chatwoot.example.com"
assert legacy.get_setting("chatwoot_account_id") == "1"
assert legacy.get_setting("chatwoot_inbox_id") == "2"
assert legacy.get_setting("chatwoot_api_token") == "smoke-chatwoot-token-must-not-render"
assert legacy.get_setting("chatwoot_enabled_at_ms").isdigit()
print("NiceGUI admin persistence smoke test passed")
