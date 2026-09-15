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
        raise SystemExit("usage: apply_phase3_transport_tests.py <patched-mautrix-meta-source-dir>")
    root = pathlib.Path(sys.argv[1]).resolve()
    test_file = root / "pkg/connector/proxy_context_test.go"
    if not test_file.is_file():
        raise SystemExit(f"expected generated connector test file: {test_file}")

    replace_once(
        test_file,
        '    "encoding/json"\n    "net/http"',
        '    "encoding/json"\n    "io"\n    "net/http"',
    )

    extra = r'''

func TestLoginProviderHTTPTransportUsesResolvedProxyAndNotDirectSentinel(t *testing.T) {
    const token = "phase3-login-transport-token"
    oldToken := os.Getenv("MAUTRIX_META_EGRESS_TOKEN")
    t.Cleanup(func() { _ = os.Setenv("MAUTRIX_META_EGRESS_TOKEN", oldToken) })
    if err := os.Setenv("MAUTRIX_META_EGRESS_TOKEN", token); err != nil { t.Fatal(err) }

    var directHits atomic.Int32
    direct := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        directHits.Add(1)
        _, _ = io.WriteString(w, "direct")
    }))
    defer direct.Close()

    var proxyHits atomic.Int32
    proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        proxyHits.Add(1)
        if r.URL.String() != direct.URL+"/" { t.Errorf("proxy received unexpected target %q", r.URL.String()) }
        _, _ = io.WriteString(w, "proxied-login")
    }))
    defer proxy.Close()

    resolver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        q := r.URL.Query()
        if q.Get("meta_account_id") != "123" || q.Get("traffic_class") != "login" || q.Get("reason") != "login" {
            t.Fatalf("wrong login resolver context: %v", q)
        }
        _ = json.NewEncoder(w).Encode(respGetProxy{ProxyURL: proxy.URL})
    }))
    defer resolver.Close()

    c := &cookies.Cookies{Platform: types.Facebook}
    c.UpdateValues(map[cookies.MetaCookieName]string{cookies.FBCookieCUser: "123"})
    conn := &MetaConnector{Config: Config{GetProxyFrom: resolver.URL, ProxyOther: true}}
    client := makePhase3TestClient(c)
    if err := conn.configureLoginProxy(client, c, true); err != nil { t.Fatal(err) }

    resp, err := client.GetHTTP().HTTP.Get(direct.URL)
    if err != nil { t.Fatal(err) }
    data, err := io.ReadAll(resp.Body)
    _ = resp.Body.Close()
    if err != nil { t.Fatal(err) }
    if string(data) != "proxied-login" { t.Fatalf("unexpected login transport body %q", data) }
    if proxyHits.Load() != 1 { t.Fatalf("expected one login proxy hit, got %d", proxyHits.Load()) }
    if directHits.Load() != 0 { t.Fatalf("login transport reached direct sentinel %d times", directHits.Load()) }
}

func TestMessagingWebsocketHTTPClientUsesResolvedProxyAndNotDirectSentinel(t *testing.T) {
    const token = "phase3-messaging-transport-token"
    oldToken := os.Getenv("MAUTRIX_META_EGRESS_TOKEN")
    t.Cleanup(func() { _ = os.Setenv("MAUTRIX_META_EGRESS_TOKEN", oldToken) })
    if err := os.Setenv("MAUTRIX_META_EGRESS_TOKEN", token); err != nil { t.Fatal(err) }

    var directHits atomic.Int32
    direct := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        directHits.Add(1)
        _, _ = io.WriteString(w, "direct")
    }))
    defer direct.Close()

    var proxyHits atomic.Int32
    proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        proxyHits.Add(1)
        if r.URL.String() != direct.URL+"/" { t.Errorf("proxy received unexpected target %q", r.URL.String()) }
        _, _ = io.WriteString(w, "proxied-websocket-client")
    }))
    defer proxy.Close()

    resolver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        q := r.URL.Query()
        if q.Get("meta_account_id") != "123" || q.Get("login_id") != "123" || q.Get("traffic_class") != "messaging" || q.Get("reason") != "reconnect-cache" {
            t.Fatalf("wrong messaging resolver context: %v", q)
        }
        _ = json.NewEncoder(w).Encode(respGetProxy{ProxyURL: proxy.URL})
    }))
    defer resolver.Close()

    c := &cookies.Cookies{Platform: types.Facebook}
    c.UpdateValues(map[cookies.MetaCookieName]string{cookies.FBCookieCUser: "123"})
    conn := &MetaConnector{Config: Config{GetProxyFrom: resolver.URL, ProxyOther: true}}
    metaClient := &MetaClient{
        Main: conn,
        Client: makePhase3TestClient(c),
        LoginMeta: &metaid.UserLoginMetadata{Platform: types.Facebook, Cookies: c},
        UserLogin: &bridgev2.UserLogin{UserLogin: &database.UserLogin{ID: "123"}},
    }
    if !metaClient.updateMessagingProxy("reconnect-cache") { t.Fatal("messaging proxy setup failed") }

    wsOpts := metaClient.Client.GetHTTP().GetWebsocketDialer()
    if wsOpts == nil || wsOpts.HTTPClient == nil { t.Fatal("websocket HTTP client is unavailable") }
    resp, err := wsOpts.HTTPClient.Get(direct.URL)
    if err != nil { t.Fatal(err) }
    data, err := io.ReadAll(resp.Body)
    _ = resp.Body.Close()
    if err != nil { t.Fatal(err) }
    if string(data) != "proxied-websocket-client" { t.Fatalf("unexpected websocket transport body %q", data) }
    if proxyHits.Load() != 1 { t.Fatalf("expected one websocket-client proxy hit, got %d", proxyHits.Load()) }
    if directHits.Load() != 0 { t.Fatalf("websocket HTTP client reached direct sentinel %d times", directHits.Load()) }
}
'''

    text = test_file.read_text()
    marker = "func TestLoginProviderHTTPTransportUsesResolvedProxyAndNotDirectSentinel"
    if marker in text:
        raise SystemExit(f"{test_file}: observable transport tests already present")
    test_file.write_text(text + extra)
    print(f"Applied Phase 3 observable transport tests to {root}")


if __name__ == "__main__":
    main()
