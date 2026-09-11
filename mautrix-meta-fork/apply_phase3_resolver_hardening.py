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


def append_once(path: pathlib.Path, marker: str, extra: str) -> None:
    text = path.read_text()
    if marker in text:
        raise SystemExit(f"{path}: resolver hardening tests already present")
    path.write_text(text + extra)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_phase3_resolver_hardening.py <patched-mautrix-meta-source-dir>")
    root = pathlib.Path(sys.argv[1]).resolve()
    client = root / "pkg/connector/client.go"
    config = root / "pkg/connector/config.go"
    login = root / "pkg/connector/login.go"
    test_file = root / "pkg/connector/proxy_context_test.go"

    replace_once(
        client,
        '"fmt"\n\t"net/http"',
        '"fmt"\n\t"io"\n\t"net/http"',
    )
    replace_once(
        client,
        '''\ttransport.Proxy = nil\n\treturn &http.Client{Transport: transport, Timeout: 5 * time.Second}\n''',
        '''\ttransport.Proxy = nil\n\treturn &http.Client{\n\t\tTransport: transport,\n\t\tTimeout:   5 * time.Second,\n\t\tCheckRedirect: func(_ *http.Request, _ []*http.Request) error {\n\t\t\treturn http.ErrUseLastResponse\n\t\t},\n\t}\n''',
    )
    replace_once(
        client,
        '''\t\tvar respData respGetProxy\n\t\terr = json.NewDecoder(resp.Body).Decode(&respData)\n\t\tif err != nil {\n\t\t\treturn "", fmt.Errorf("failed to decode response: %w", err)\n\t\t}\n\t\tproxyURL, err := url.Parse(respData.ProxyURL)\n\t\tif err != nil || proxyURL.Scheme == "" || proxyURL.Host == "" {\n\t\t\treturn "", fmt.Errorf("proxy resolver returned invalid proxy URL")\n\t\t}\n\t\treturn respData.ProxyURL, nil\n''',
        '''\t\tconst maxResolverResponseBytes = 64 * 1024\n\t\tbody, err := io.ReadAll(io.LimitReader(resp.Body, maxResolverResponseBytes+1))\n\t\tif err != nil {\n\t\t\treturn "", fmt.Errorf("failed to read response: %w", err)\n\t\t} else if len(body) > maxResolverResponseBytes {\n\t\t\treturn "", fmt.Errorf("proxy resolver response exceeds size limit")\n\t\t}\n\t\tvar respData respGetProxy\n\t\tif err = json.Unmarshal(body, &respData); err != nil {\n\t\t\treturn "", fmt.Errorf("failed to decode response: %w", err)\n\t\t}\n\t\tproxyURL, err := url.Parse(respData.ProxyURL)\n\t\tif err != nil || proxyURL.Hostname() == "" || proxyURL.Port() == "" || proxyURL.Path != "" || proxyURL.RawQuery != "" || proxyURL.Fragment != "" {\n\t\t\treturn "", fmt.Errorf("proxy resolver returned invalid proxy URL")\n\t\t}\n\t\tswitch proxyURL.Scheme {\n\t\tcase "http", "https", "socks5":\n\t\tdefault:\n\t\t\treturn "", fmt.Errorf("proxy resolver returned unsupported proxy scheme")\n\t\t}\n\t\treturn respData.ProxyURL, nil\n''',
    )

    replace_once(
        config,
        '"fmt"\n\t"strings"',
        '"fmt"\n\t"net/url"\n\t"strings"',
    )
    replace_once(
        config,
        '''\tif m.Config.GetProxyFrom != "" {\n\t\tif m.Config.Proxy != "" {\n''',
        '''\tif m.Config.GetProxyFrom != "" {\n\t\tresolverURL, err := url.Parse(m.Config.GetProxyFrom)\n\t\tif err != nil || resolverURL.Host == "" || (resolverURL.Scheme != "http" && resolverURL.Scheme != "https") || resolverURL.User != nil || resolverURL.RawQuery != "" || resolverURL.Fragment != "" {\n\t\t\treturn fmt.Errorf("dynamic egress resolver URL must be an http(s) URL without userinfo, query or fragment")\n\t\t}\n\t\tif m.Config.Proxy != "" {\n''',
    )

    replace_once(
        login,
        '''\tmetaClient.Client = client\n\n\tbackgroundCtx := ul.Log.WithContext(conn.Bridge.BackgroundCtx)\n''',
        '''\tmetaClient.Client = client\n\tif !metaClient.updateMessagingProxy("connect") {\n\t\treturn nil, fmt.Errorf("failed to transition from login to messaging proxy")\n\t}\n\n\tbackgroundCtx := ul.Log.WithContext(conn.Bridge.BackgroundCtx)\n''',
    )

    append_once(
        test_file,
        "TestResolverRejectsRedirectsWithoutFollowing",
        r'''

func TestResolverRejectsRedirectsWithoutFollowing(t *testing.T) {
    const token = "phase3-redirect-test-token"
    oldToken := os.Getenv("MAUTRIX_META_EGRESS_TOKEN")
    t.Cleanup(func() { _ = os.Setenv("MAUTRIX_META_EGRESS_TOKEN", oldToken) })
    if err := os.Setenv("MAUTRIX_META_EGRESS_TOKEN", token); err != nil { t.Fatal(err) }

    var redirectedHits atomic.Int32
    redirected := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        redirectedHits.Add(1)
        _ = json.NewEncoder(w).Encode(respGetProxy{ProxyURL: "http://127.0.0.1:8080"})
    }))
    defer redirected.Close()
    resolver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        http.Redirect(w, r, redirected.URL, http.StatusTemporaryRedirect)
    }))
    defer resolver.Close()

    conn := &MetaConnector{Config: Config{GetProxyFrom: resolver.URL}}
    _, err := conn.getProxy(ProxyContext{MetaAccountID: "123", TrafficClass: ProxyTrafficMessaging})("connect")
    if err == nil { t.Fatal("expected redirecting resolver to fail closed") }
    if redirectedHits.Load() != 0 { t.Fatalf("resolver redirect target was reached %d times", redirectedHits.Load()) }
}

func TestResolverRejectsOversizedResponse(t *testing.T) {
    const token = "phase3-oversize-test-token"
    oldToken := os.Getenv("MAUTRIX_META_EGRESS_TOKEN")
    t.Cleanup(func() { _ = os.Setenv("MAUTRIX_META_EGRESS_TOKEN", oldToken) })
    if err := os.Setenv("MAUTRIX_META_EGRESS_TOKEN", token); err != nil { t.Fatal(err) }

    resolver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        _, _ = w.Write(make([]byte, 64*1024+1))
    }))
    defer resolver.Close()

    conn := &MetaConnector{Config: Config{GetProxyFrom: resolver.URL}}
    _, err := conn.getProxy(ProxyContext{MetaAccountID: "123", TrafficClass: ProxyTrafficMessaging})("connect")
    if err == nil { t.Fatal("expected oversized resolver response to fail closed") }
}

func TestResolverRejectsMalformedOrUnsupportedProxyURLs(t *testing.T) {
    const token = "phase3-proxy-url-test-token"
    oldToken := os.Getenv("MAUTRIX_META_EGRESS_TOKEN")
    t.Cleanup(func() { _ = os.Setenv("MAUTRIX_META_EGRESS_TOKEN", oldToken) })
    if err := os.Setenv("MAUTRIX_META_EGRESS_TOKEN", token); err != nil { t.Fatal(err) }

    for _, returned := range []string{
        "ftp://proxy.test:21",
        "http://proxy.test:8080/path",
        "http://proxy.test:8080?x=1",
        "http://proxy.test",
    } {
        t.Run(returned, func(t *testing.T) {
            resolver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
                _ = json.NewEncoder(w).Encode(respGetProxy{ProxyURL: returned})
            }))
            defer resolver.Close()
            conn := &MetaConnector{Config: Config{GetProxyFrom: resolver.URL}}
            if _, err := conn.getProxy(ProxyContext{MetaAccountID: "123", TrafficClass: ProxyTrafficMessaging})("connect"); err == nil {
                t.Fatalf("expected resolver proxy URL %q to fail closed", returned)
            }
        })
    }
}

func TestDynamicEgressConfigRejectsUnsafeResolverURLs(t *testing.T) {
    for _, resolverURL := range []string{
        "ftp://control-plane/internal/v1/egress/resolve",
        "http://token@control-plane/internal/v1/egress/resolve",
        "http://control-plane/internal/v1/egress/resolve?token=bad",
        "http://control-plane/internal/v1/egress/resolve#fragment",
        "control-plane/internal/v1/egress/resolve",
    } {
        conn := &MetaConnector{}
        conn.Config.RawMode = "facebook"
        conn.Config.Mode = types.Facebook
        conn.Config.GetProxyFrom = resolverURL
        conn.Config.ProxyOther = true
        conn.Config.ProxyMedia = true
        conn.Config.ProxyE2EE = true
        if err := conn.ValidateConfig(); err == nil {
            t.Fatalf("expected unsafe resolver URL %q to be rejected", resolverURL)
        }
    }
}

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
''',
    )
    print(f"Applied Phase 3 resolver hardening to {root}")


if __name__ == "__main__":
    main()
