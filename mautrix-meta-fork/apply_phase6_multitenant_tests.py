#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_phase6_multitenant_tests.py <patched-mautrix-meta-source-dir>")
    root = pathlib.Path(sys.argv[1]).resolve()
    test_file = root / "pkg/connector/proxy_context_test.go"
    if not test_file.is_file():
        raise SystemExit(f"expected generated connector test file: {test_file}")
    text = test_file.read_text()
    marker = "func TestPhase6TwoAccountsUseDistinctResolvedTransports"
    if marker in text:
        raise SystemExit(f"{test_file}: Phase 6 multi-account test already present")

    # Phase 3's generated test file already imports the connector dependencies we
    # need. Add networkid to the existing import block for the typed UserLogin ID.
    import_anchor = '"maunium.net/go/mautrix/bridgev2/database"\n'
    if import_anchor not in text:
        raise SystemExit(f"{test_file}: expected database import anchor")
    text = text.replace(import_anchor, import_anchor + '\t"maunium.net/go/mautrix/bridgev2/networkid"\n', 1)

    extra = r'''

func TestPhase6TwoAccountsUseDistinctResolvedTransports(t *testing.T) {
    const token = "phase6-multitenant-transport-token"
    oldToken := os.Getenv("MAUTRIX_META_EGRESS_TOKEN")
    t.Cleanup(func() { _ = os.Setenv("MAUTRIX_META_EGRESS_TOKEN", oldToken) })
    if err := os.Setenv("MAUTRIX_META_EGRESS_TOKEN", token); err != nil { t.Fatal(err) }

    var directHits atomic.Int32
    direct := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        directHits.Add(1)
        _, _ = io.WriteString(w, "direct")
    }))
    defer direct.Close()

    var proxyAHits atomic.Int32
    proxyA := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        proxyAHits.Add(1)
        if r.URL.String() != direct.URL+"/" { t.Errorf("proxy A received unexpected target %q", r.URL.String()) }
        _, _ = io.WriteString(w, "tenant-a")
    }))
    defer proxyA.Close()

    var proxyBHits atomic.Int32
    proxyB := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        proxyBHits.Add(1)
        if r.URL.String() != direct.URL+"/" { t.Errorf("proxy B received unexpected target %q", r.URL.String()) }
        _, _ = io.WriteString(w, "tenant-b")
    }))
    defer proxyB.Close()

    var resolverAHits atomic.Int32
    var resolverBHits atomic.Int32
    resolver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        if r.Header.Get("Authorization") != "Bearer "+token { t.Fatalf("resolver auth mismatch") }
        q := r.URL.Query()
        account := q.Get("meta_account_id")
        loginID := q.Get("login_id")
        trafficClass := q.Get("traffic_class")
        if trafficClass == "login" && loginID != "" {
            t.Fatalf("first-login must not claim a persisted login ID yet: %v", q)
        }
        if trafficClass == "messaging" && loginID != account {
            t.Fatalf("established messaging must carry account and login identity: %v", q)
        }
        if account == "111" {
            resolverAHits.Add(1)
            _ = json.NewEncoder(w).Encode(respGetProxy{ProxyURL: proxyA.URL})
            return
        }
        if account == "222" {
            resolverBHits.Add(1)
            _ = json.NewEncoder(w).Encode(respGetProxy{ProxyURL: proxyB.URL})
            return
        }
        t.Fatalf("unexpected account context: %v", q)
    }))
    defer resolver.Close()

    makeAccount := func(id string) (*cookies.Cookies, *MetaClient) {
        c := &cookies.Cookies{Platform: types.Facebook}
        c.UpdateValues(map[cookies.MetaCookieName]string{cookies.FBCookieCUser: id})
        conn := &MetaConnector{Config: Config{GetProxyFrom: resolver.URL, ProxyOther: true}}
        mc := &MetaClient{
            Main: conn,
            Client: makePhase3TestClient(c),
            LoginMeta: &metaid.UserLoginMetadata{Platform: types.Facebook, Cookies: c},
            UserLogin: &bridgev2.UserLogin{UserLogin: &database.UserLogin{ID: networkid.UserLoginID(id)}},
        }
        return c, mc
    }

    cookiesA, clientA := makeAccount("111")
    cookiesB, clientB := makeAccount("222")

    // First-login HTTP clients must select account-specific transports using only
    // the provider identity that exists before BridgeV2 persists UserLogin.ID.
    if err := clientA.Main.configureLoginProxy(clientA.Client, cookiesA, true); err != nil { t.Fatal(err) }
    if err := clientB.Main.configureLoginProxy(clientB.Client, cookiesB, true); err != nil { t.Fatal(err) }
    for name, mc := range map[string]*MetaClient{"A": clientA, "B": clientB} {
        resp, err := mc.Client.GetHTTP().HTTP.Get(direct.URL)
        if err != nil { t.Fatalf("tenant %s login request failed: %v", name, err) }
        body, err := io.ReadAll(resp.Body)
        _ = resp.Body.Close()
        if err != nil { t.Fatal(err) }
        expected := "tenant-a"
        if name == "B" { expected = "tenant-b" }
        if string(body) != expected { t.Fatalf("tenant %s login used wrong proxy: %q", name, body) }
    }

    // Established/reconnect websocket HTTP clients must add UserLogin.ID and
    // preserve the same tenant split.
    if !clientA.updateMessagingProxy("reconnect-cache") { t.Fatal("tenant A messaging proxy setup failed") }
    if !clientB.updateMessagingProxy("reconnect-cache") { t.Fatal("tenant B messaging proxy setup failed") }
    for name, mc := range map[string]*MetaClient{"A": clientA, "B": clientB} {
        opts := mc.Client.GetHTTP().GetWebsocketDialer()
        if opts == nil || opts.HTTPClient == nil { t.Fatalf("tenant %s websocket client unavailable", name) }
        resp, err := opts.HTTPClient.Get(direct.URL)
        if err != nil { t.Fatalf("tenant %s messaging request failed: %v", name, err) }
        body, err := io.ReadAll(resp.Body)
        _ = resp.Body.Close()
        if err != nil { t.Fatal(err) }
        expected := "tenant-a"
        if name == "B" { expected = "tenant-b" }
        if string(body) != expected { t.Fatalf("tenant %s messaging used wrong proxy: %q", name, body) }
    }

    if directHits.Load() != 0 { t.Fatalf("multi-tenant protected transports reached direct sentinel %d times", directHits.Load()) }
    if proxyAHits.Load() != 2 || proxyBHits.Load() != 2 { t.Fatalf("wrong proxy hit distribution A=%d B=%d", proxyAHits.Load(), proxyBHits.Load()) }
    if resolverAHits.Load() != 2 || resolverBHits.Load() != 2 { t.Fatalf("wrong resolver context distribution A=%d B=%d", resolverAHits.Load(), resolverBHits.Load()) }
}
'''
    test_file.write_text(text + extra)
    print(f"Applied Phase 6 multi-account transport proof to {root}")


if __name__ == "__main__":
    main()
