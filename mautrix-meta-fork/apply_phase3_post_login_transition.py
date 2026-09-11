#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys


def replace_once(path: pathlib.Path, old: str, new: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one occurrence of {old!r}, found {count}")
    path.write_text(text.replace(old, new, 1))


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_phase3_post_login_transition.py <patched-mautrix-meta-source-dir>")
    root = pathlib.Path(sys.argv[1]).resolve()
    login = root / "pkg/connector/login.go"
    test_file = root / "pkg/connector/proxy_context_test.go"

    replace_once(
        login,
        '''\tmetaClient.Client = client\n\n\tbackgroundCtx := ul.Log.WithContext(conn.Bridge.BackgroundCtx)\n''',
        '''\tmetaClient.Client = client\n\tif !metaClient.updateMessagingProxy("connect") {\n\t\treturn nil, fmt.Errorf("failed to transition from login to messaging proxy")\n\t}\n\n\tbackgroundCtx := ul.Log.WithContext(conn.Bridge.BackgroundCtx)\n''',
    )

    text = test_file.read_text()
    marker = "func TestInitialLoginTransitionsResolverFromLoginToMessaging"
    if marker in text:
        raise SystemExit(f"{test_file}: post-login transition test already present")
    test_file.write_text(text + r'''

func TestInitialLoginTransitionsResolverFromLoginToMessaging(t *testing.T) {
    const token = "phase3-login-transition-token"
    oldToken := os.Getenv("MAUTRIX_META_EGRESS_TOKEN")
    t.Cleanup(func() { _ = os.Setenv("MAUTRIX_META_EGRESS_TOKEN", oldToken) })
    if err := os.Setenv("MAUTRIX_META_EGRESS_TOKEN", token); err != nil { t.Fatal(err) }

    var classes []string
    var reasons []string
    server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        classes = append(classes, r.URL.Query().Get("traffic_class"))
        reasons = append(reasons, r.URL.Query().Get("reason"))
        _ = json.NewEncoder(w).Encode(respGetProxy{ProxyURL: "http://127.0.0.1:9"})
    }))
    defer server.Close()

    c := &cookies.Cookies{Platform: types.Facebook}
    c.UpdateValues(map[cookies.MetaCookieName]string{cookies.FBCookieCUser: "123"})
    conn := &MetaConnector{Config: Config{GetProxyFrom: server.URL, ProxyOther: true}}
    client := makePhase3TestClient(c)
    if err := conn.configureLoginProxy(client, c, true); err != nil { t.Fatal(err) }

    metaClient := &MetaClient{
        Main: conn,
        Client: client,
        LoginMeta: &metaid.UserLoginMetadata{Platform: types.Facebook, Cookies: c},
        UserLogin: &bridgev2.UserLogin{UserLogin: &database.UserLogin{ID: "123"}},
    }
    if !metaClient.updateMessagingProxy("connect") { t.Fatal("messaging proxy transition failed") }

    if len(classes) != 2 || classes[0] != "login" || classes[1] != "messaging" {
        t.Fatalf("unexpected resolver traffic-class sequence: %v", classes)
    }
    if len(reasons) != 2 || reasons[0] != "login" || reasons[1] != "connect" {
        t.Fatalf("unexpected resolver reason sequence: %v", reasons)
    }
}
''')
    print(f"Applied Phase 3 post-login traffic transition to {root}")


if __name__ == "__main__":
    main()
