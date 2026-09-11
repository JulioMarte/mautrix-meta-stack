#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys


def replace_exact(path: pathlib.Path, old: str, new: str, expected: int) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != expected:
        raise SystemExit(f"{path}: expected {expected} occurrences of {old!r}, found {count}")
    path.write_text(text.replace(old, new))


def append_once(path: pathlib.Path, marker: str, extra: str) -> None:
    text = path.read_text()
    if extra.strip() in text:
        raise SystemExit(f"{path}: hardening test already present")
    if marker not in text:
        raise SystemExit(f"{path}: expected marker not found")
    path.write_text(text + extra)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_phase3_fixups.py <patched-mautrix-meta-source-dir>")
    root = pathlib.Path(sys.argv[1]).resolve()

    replace_exact(root / "pkg/connector/chatinfo.go", "wrapAvatar(", "m.wrapAvatar(", 2)
    replace_exact(root / "pkg/connector/handlemeta.go", "wrapAvatar(evt.ImageURL)", "m.wrapAvatar(evt.ImageURL)", 1)

    media_test = root / "pkg/msgconv/mediadl/proxy_context_test.go"
    replace_exact(
        media_test,
        "mediaHTTPClient = &http.Client{Transport: http.DefaultTransport.(*http.Transport).Clone()}",
        "mediaHTTPClient = &http.Client{Transport: &http.Transport{}}",
        1,
    )

    client = root / "pkg/connector/client.go"
    replace_exact(
        client,
        'type respGetProxy struct {\n\tProxyURL string `json:"proxy_url"`\n}\n',
        '''type respGetProxy struct {\n\tProxyURL string `json:"proxy_url"`\n}\n\nvar proxyResolverHTTPClient = func() *http.Client {\n\ttransport := http.DefaultTransport.(*http.Transport).Clone()\n\ttransport.Proxy = nil\n\treturn &http.Client{Transport: transport, Timeout: 5 * time.Second}\n}()\n''',
        1,
    )
    replace_exact(client, "resp, err := http.DefaultClient.Do(req)", "resp, err := proxyResolverHTTPClient.Do(req)", 1)

    replace_exact(
        client,
        '''func (m *MetaClient) proxyContext(trafficClass ProxyTrafficClass) ProxyContext {\n\tmetaAccountID := ""\n\tif m.LoginMeta != nil && m.LoginMeta.Cookies != nil && m.LoginMeta.Cookies.GetUserID() != 0 {\n\t\tmetaAccountID = fmt.Sprint(m.LoginMeta.Cookies.GetUserID())\n\t}\n\treturn ProxyContext{\n\t\tMetaAccountID: metaAccountID,\n\t\tLoginID:       string(m.UserLogin.ID),\n\t\tTrafficClass:  trafficClass,\n\t}\n}\n''',
        '''func (m *MetaClient) proxyContext(trafficClass ProxyTrafficClass) ProxyContext {\n\tmetaAccountID := ""\n\tif m.LoginMeta != nil && m.LoginMeta.Cookies != nil && m.LoginMeta.Cookies.GetUserID() != 0 {\n\t\tmetaAccountID = fmt.Sprint(m.LoginMeta.Cookies.GetUserID())\n\t}\n\tloginID := ""\n\tif m.UserLogin != nil && m.UserLogin.UserLogin != nil {\n\t\tloginID = string(m.UserLogin.ID)\n\t}\n\treturn ProxyContext{\n\t\tMetaAccountID: metaAccountID,\n\t\tLoginID:       loginID,\n\t\tTrafficClass:  trafficClass,\n\t}\n}\n\nfunc (m *MetaClient) updateMessagingProxy(reason string) bool {\n\tif !m.Main.Config.ProxyOther || (m.Main.Config.GetProxyFrom == "" && m.Main.Config.Proxy == "") {\n\t\treturn true\n\t}\n\tif m.Client == nil {\n\t\treturn false\n\t}\n\tm.Client.GetHTTP().GetNewProxy = m.Main.getProxy(m.proxyContext(ProxyTrafficMessaging))\n\treturn m.Client.GetHTTP().UpdateProxy(reason)\n}\n''',
        1,
    )

    replace_exact(
        client,
        '''\t\t} else {\n\t\t\tzerolog.Ctx(ctx).Debug().\n\t\t\t\tTime("last_used", lastUsed).\n\t\t\t\tMsg("Reconnecting with cached state")\n\t\t\tm.connectWithCache(ctx)\n\t\t\treturn\n\t\t}\n''',
        '''\t\t} else {\n\t\t\tzerolog.Ctx(ctx).Debug().\n\t\t\t\tTime("last_used", lastUsed).\n\t\t\t\tMsg("Reconnecting with cached state")\n\t\t\tif !m.updateMessagingProxy("reconnect-cache") {\n\t\t\t\tm.UserLogin.BridgeState.Send(status.BridgeState{\n\t\t\t\t\tStateEvent: status.StateUnknownError,\n\t\t\t\t\tError:      MetaProxyUpdateFail,\n\t\t\t\t})\n\t\t\t\treturn\n\t\t\t}\n\t\t\tm.connectWithCache(ctx)\n\t\t\treturn\n\t\t}\n''',
        1,
    )
    replace_exact(
        client,
        '''\tif m.Main.Config.ProxyOther && (m.Main.Config.GetProxyFrom != "" || m.Main.Config.Proxy != "") {\n\t\tcli.GetHTTP().GetNewProxy = m.Main.getProxy(m.proxyContext(ProxyTrafficMessaging))\n\t\tif !cli.GetHTTP().UpdateProxy("connect") {\n\t\t\tm.UserLogin.BridgeState.Send(status.BridgeState{\n\t\t\t\tStateEvent: status.StateUnknownError,\n\t\t\t\tError:      MetaProxyUpdateFail,\n\t\t\t})\n\t\t\treturn\n\t\t}\n\t}\n''',
        '''\tif !m.updateMessagingProxy("connect") {\n\t\tm.UserLogin.BridgeState.Send(status.BridgeState{\n\t\t\tStateEvent: status.StateUnknownError,\n\t\t\tError:      MetaProxyUpdateFail,\n\t\t})\n\t\treturn\n\t}\n''',
        1,
    )

    login = root / "pkg/connector/login.go"
    replace_exact(
        login,
        '''func getMessagixClient(log zerolog.Logger, conn *MetaConnector, c *cookies.Cookies, useProxy bool) (*messagix.Client, error) {\n\tclient := messagix.NewClient(c, log, conn.getMessagixConfig())\n\tif useProxy && (conn.Config.GetProxyFrom != "" || conn.Config.Proxy != "") {\n\t\tmetaAccountID := c.GetUserID()\n\t\tif conn.Config.GetProxyFrom != "" && metaAccountID == 0 {\n\t\t\treturn nil, fmt.Errorf("stable account identity is required before proxy-enabled login")\n\t\t}\n\t\tclient.GetHTTP().GetNewProxy = conn.getProxy(ProxyContext{\n\t\t\tMetaAccountID: fmt.Sprint(metaAccountID),\n\t\t\tTrafficClass:  ProxyTrafficLogin,\n\t\t})\n\t\tif !client.GetHTTP().UpdateProxy("login") {\n\t\t\treturn nil, fmt.Errorf("failed to update proxy")\n\t\t}\n\t}\n\treturn client, nil\n}\n''',
        '''func (conn *MetaConnector) configureLoginProxy(client *messagix.Client, c *cookies.Cookies, useProxy bool) error {\n\tif !useProxy || (conn.Config.GetProxyFrom == "" && conn.Config.Proxy == "") {\n\t\treturn nil\n\t}\n\tmetaAccountID := c.GetUserID()\n\tif conn.Config.GetProxyFrom != "" && metaAccountID == 0 {\n\t\treturn fmt.Errorf("stable account identity is required before proxy-enabled login")\n\t}\n\tclient.GetHTTP().GetNewProxy = conn.getProxy(ProxyContext{\n\t\tMetaAccountID: fmt.Sprint(metaAccountID),\n\t\tTrafficClass:  ProxyTrafficLogin,\n\t})\n\tif !client.GetHTTP().UpdateProxy("login") {\n\t\treturn fmt.Errorf("failed to update proxy")\n\t}\n\treturn nil\n}\n\nfunc getMessagixClient(log zerolog.Logger, conn *MetaConnector, c *cookies.Cookies, useProxy bool) (*messagix.Client, error) {\n\tclient := messagix.NewClient(c, log, conn.getMessagixConfig())\n\tif err := conn.configureLoginProxy(client, c, useProxy); err != nil {\n\t\treturn nil, err\n\t}\n\treturn client, nil\n}\n''',
        1,
    )

    config = root / "pkg/connector/config.go"
    replace_exact(
        config,
        '''func (m *MetaConnector) ValidateConfig() error {\n\tif m.Config.Mode == types.Unset && m.Config.RawMode != "" {\n\t\treturn fmt.Errorf("invalid mode %q", m.Config.RawMode)\n\t}\n\treturn nil\n}\n''',
        '''func (m *MetaConnector) ValidateConfig() error {\n\tif m.Config.Mode == types.Unset && m.Config.RawMode != "" {\n\t\treturn fmt.Errorf("invalid mode %q", m.Config.RawMode)\n\t}\n\tif m.Config.GetProxyFrom != "" {\n\t\tif m.Config.Proxy != "" {\n\t\t\treturn fmt.Errorf("dynamic egress cannot be combined with a static proxy fallback")\n\t\t}\n\t\tif !m.Config.ProxyOther || !m.Config.ProxyMedia || !m.Config.ProxyE2EE {\n\t\t\treturn fmt.Errorf("dynamic egress requires proxy_other, proxy_media and proxy_e2ee")\n\t\t}\n\t\tif m.Config.ProxyMessengerLite {\n\t\t\treturn fmt.Errorf("dynamic egress does not support proxy_messenger_lite before stable login identity exists")\n\t\t}\n\t\tif len(m.Config.AllowedModes) == 0 {\n\t\t\tif m.Config.Mode != types.Facebook && m.Config.Mode != types.Messenger {\n\t\t\t\treturn fmt.Errorf("dynamic egress currently supports only facebook and messenger modes")\n\t\t\t}\n\t\t} else {\n\t\t\tfor _, mode := range m.Config.AllowedModes {\n\t\t\t\tif mode != types.Facebook && mode != types.Messenger {\n\t\t\t\t\treturn fmt.Errorf("dynamic egress allowed_modes currently supports only facebook and messenger")\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n\treturn nil\n}\n''',
        1,
    )

    download = root / "pkg/msgconv/mediadl/download.go"
    replace_exact(
        download,
        "var mediaHTTPClient *http.Client\nvar BypassOnionForMedia bool\n",
        "var mediaHTTPClient *http.Client\nvar BypassOnionForMedia bool\nvar RequireProxyContext bool\n",
        1,
    )
    replace_exact(
        download,
        '''\tproxyURL, _ := ctx.Value(ContextKeyProxyURL).(string)\n\tif proxyURL == "" {\n\t\treturn mediaHTTPClient, nil\n\t}\n''',
        '''\tproxyURL, _ := ctx.Value(ContextKeyProxyURL).(string)\n\tif proxyURL == "" {\n\t\tif RequireProxyContext {\n\t\t\treturn nil, fmt.Errorf("media proxy context is required")\n\t\t}\n\t\treturn mediaHTTPClient, nil\n\t}\n''',
        1,
    )

    connector = root / "pkg/connector/connector.go"
    replace_exact(
        connector,
        '''\tcfg := m.Bridge.GetHTTPClientSettings()\n\tmediadl.SetHTTP(cfg)\n''',
        '''\tcfg := m.Bridge.GetHTTPClientSettings()\n\tmediadl.SetHTTP(cfg)\n\tmediadl.RequireProxyContext = m.Config.GetProxyFrom != "" && m.Config.ProxyMedia\n''',
        1,
    )

    connector_test = root / "pkg/connector/proxy_context_test.go"
    append_once(
        connector_test,
        "TestDynamicProxyResolverFailsWithoutIdentityOrToken",
        r'''

func makePhase3TestClient(c *cookies.Cookies) *messagix.Client {
    log := zerolog.Nop()
    return messagix.NewClient(c, log, &messagix.Config{ClientSettings: exhttp.SensibleClientSettings})
}

func TestLoginProxySetupUsesCookieIdentityBeforeProviderRequest(t *testing.T) {
    const token = "phase3-login-path-token"
    oldToken := os.Getenv("MAUTRIX_META_EGRESS_TOKEN")
    t.Cleanup(func() { _ = os.Setenv("MAUTRIX_META_EGRESS_TOKEN", oldToken) })
    if err := os.Setenv("MAUTRIX_META_EGRESS_TOKEN", token); err != nil { t.Fatal(err) }

    var hits atomic.Int32
    server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        hits.Add(1)
        if r.URL.Query().Get("meta_account_id") != "123" { t.Fatalf("missing cookie account identity") }
        if r.URL.Query().Get("traffic_class") != "login" { t.Fatalf("wrong traffic class") }
        if r.URL.Query().Get("reason") != "login" { t.Fatalf("wrong login reason") }
        _ = json.NewEncoder(w).Encode(respGetProxy{ProxyURL: "http://127.0.0.1:9"})
    }))
    defer server.Close()

    c := &cookies.Cookies{Platform: types.Facebook}
    c.UpdateValues(map[cookies.MetaCookieName]string{cookies.FBCookieCUser: "123"})
    conn := &MetaConnector{Config: Config{GetProxyFrom: server.URL, ProxyOther: true}}
    client := makePhase3TestClient(c)
    if err := conn.configureLoginProxy(client, c, true); err != nil { t.Fatal(err) }
    if hits.Load() != 1 { t.Fatalf("expected exactly one pre-provider resolver call, got %d", hits.Load()) }
}

func TestCachedReconnectProxySetupUsesStableAccountAndLoginIdentity(t *testing.T) {
    const token = "phase3-reconnect-path-token"
    oldToken := os.Getenv("MAUTRIX_META_EGRESS_TOKEN")
    t.Cleanup(func() { _ = os.Setenv("MAUTRIX_META_EGRESS_TOKEN", oldToken) })
    if err := os.Setenv("MAUTRIX_META_EGRESS_TOKEN", token); err != nil { t.Fatal(err) }

    var hits atomic.Int32
    server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        hits.Add(1)
        q := r.URL.Query()
        if q.Get("meta_account_id") != "123" || q.Get("login_id") != "123" { t.Fatalf("wrong reconnect identity: %v", q) }
        if q.Get("traffic_class") != "messaging" || q.Get("reason") != "reconnect-cache" { t.Fatalf("wrong reconnect context: %v", q) }
        _ = json.NewEncoder(w).Encode(respGetProxy{ProxyURL: "http://127.0.0.1:9"})
    }))
    defer server.Close()

    c := &cookies.Cookies{Platform: types.Facebook}
    c.UpdateValues(map[cookies.MetaCookieName]string{cookies.FBCookieCUser: "123"})
    conn := &MetaConnector{Config: Config{GetProxyFrom: server.URL, ProxyOther: true}}
    metaClient := &MetaClient{
        Main: conn,
        Client: makePhase3TestClient(c),
        LoginMeta: &metaid.UserLoginMetadata{Platform: types.Facebook, Cookies: c},
        UserLogin: &bridgev2.UserLogin{UserLogin: &database.UserLogin{ID: "123"}},
    }
    if !metaClient.updateMessagingProxy("reconnect-cache") { t.Fatal("cached reconnect proxy update failed") }
    if hits.Load() != 1 { t.Fatalf("expected exactly one cached reconnect resolver call, got %d", hits.Load()) }
}

func TestDynamicProxyResolverHasBoundedTimeout(t *testing.T) {
    const token = "phase3-timeout-test-token"
    oldToken := os.Getenv("MAUTRIX_META_EGRESS_TOKEN")
    oldClient := proxyResolverHTTPClient
    t.Cleanup(func() {
        _ = os.Setenv("MAUTRIX_META_EGRESS_TOKEN", oldToken)
        proxyResolverHTTPClient = oldClient
    })
    if err := os.Setenv("MAUTRIX_META_EGRESS_TOKEN", token); err != nil { t.Fatal(err) }
    proxyResolverHTTPClient = &http.Client{Timeout: 50 * time.Millisecond}

    server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        time.Sleep(250 * time.Millisecond)
    }))
    defer server.Close()

    conn := &MetaConnector{}
    conn.Config.GetProxyFrom = server.URL
    started := time.Now()
    _, err := conn.getProxy(ProxyContext{MetaAccountID: "123", TrafficClass: ProxyTrafficMessaging})("connect")
    if err == nil { t.Fatal("expected resolver timeout") }
    if elapsed := time.Since(started); elapsed > 200*time.Millisecond {
        t.Fatalf("resolver timeout was not bounded: %s", elapsed)
    }
}

func TestResolverClientIgnoresAmbientProxyEnvironment(t *testing.T) {
    transport, ok := proxyResolverHTTPClient.Transport.(*http.Transport)
    if !ok { t.Fatalf("unexpected resolver transport type %T", proxyResolverHTTPClient.Transport) }
    if transport.Proxy != nil { t.Fatal("resolver transport must not use ProxyFromEnvironment") }
}

func TestDynamicEgressConfigRejectsPartialProxyCoverage(t *testing.T) {
    conn := &MetaConnector{}
    conn.Config.RawMode = "facebook"
    conn.Config.Mode = types.Facebook
    conn.Config.GetProxyFrom = "http://control-plane/internal/v1/egress/resolve"
    conn.Config.ProxyOther = true
    conn.Config.ProxyMedia = true
    conn.Config.ProxyE2EE = false
    if err := conn.ValidateConfig(); err == nil { t.Fatal("expected partial dynamic egress configuration to be rejected") }
}

func TestDynamicEgressConfigRejectsStaticFallback(t *testing.T) {
    conn := &MetaConnector{}
    conn.Config.RawMode = "facebook"
    conn.Config.Mode = types.Facebook
    conn.Config.GetProxyFrom = "http://control-plane/internal/v1/egress/resolve"
    conn.Config.Proxy = "socks5://fallback.test:1080"
    conn.Config.ProxyOther = true
    conn.Config.ProxyMedia = true
    conn.Config.ProxyE2EE = true
    if err := conn.ValidateConfig(); err == nil { t.Fatal("expected static fallback with dynamic egress to be rejected") }
}

func TestDynamicEgressConfigRejectsUnsupportedIdentityModes(t *testing.T) {
    for _, mode := range []types.Platform{types.Instagram, types.MessengerLite, types.FacebookTor} {
        conn := &MetaConnector{}
        conn.Config.RawMode = mode.String()
        conn.Config.Mode = mode
        conn.Config.GetProxyFrom = "http://control-plane/internal/v1/egress/resolve"
        conn.Config.ProxyOther = true
        conn.Config.ProxyMedia = true
        conn.Config.ProxyE2EE = true
        if err := conn.ValidateConfig(); err == nil { t.Fatalf("expected dynamic egress mode %s to be rejected", mode) }
    }
}
''',
    )
    replace_exact(
        connector_test,
        '"os"\n    "testing"',
        '''"os"\n    "sync/atomic"\n    "testing"\n    "time"\n\n    "github.com/rs/zerolog"\n    "go.mau.fi/util/exhttp"\n    "maunium.net/go/mautrix/bridgev2"\n    "maunium.net/go/mautrix/bridgev2/database"\n\n    "go.mau.fi/mautrix-meta/pkg/messagix"\n    "go.mau.fi/mautrix-meta/pkg/messagix/cookies"\n    "go.mau.fi/mautrix-meta/pkg/messagix/types"\n    "go.mau.fi/mautrix-meta/pkg/metaid"''',
        1,
    )

    append_once(
        media_test,
        "TestClientForContextUsesRequestScopedTransport",
        r'''

func TestClientForContextFailsClosedWithoutProxyWhenRequired(t *testing.T) {
    old := RequireProxyContext
    RequireProxyContext = true
    t.Cleanup(func() { RequireProxyContext = old })
    if _, err := clientForContext(context.Background()); err == nil {
        t.Fatal("expected missing media proxy context to fail closed")
    }
}

func TestDownloadMediaUsesScopedProxyAndNeverHitsDirectSentinel(t *testing.T) {
    oldClient := mediaHTTPClient
    oldRequired := RequireProxyContext
    mediaHTTPClient = &http.Client{Transport: &http.Transport{}}
    RequireProxyContext = true
    t.Cleanup(func() {
        mediaHTTPClient = oldClient
        RequireProxyContext = oldRequired
    })

    var directHits atomic.Int32
    direct := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        directHits.Add(1)
        _, _ = io.WriteString(w, "direct")
    }))
    defer direct.Close()

    var proxyHits atomic.Int32
    proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        proxyHits.Add(1)
        if r.URL.String() != direct.URL+"/" {
            t.Errorf("proxy received unexpected target %q", r.URL.String())
        }
        _, _ = io.WriteString(w, "proxied")
    }))
    defer proxy.Close()

    ctx := WithProxy(context.Background(), proxy.URL)
    _, body, err := DownloadMedia(ctx, "image/jpeg", direct.URL, 1024)
    if err != nil { t.Fatal(err) }
    data, err := io.ReadAll(body)
    _ = body.Close()
    if err != nil { t.Fatal(err) }
    if string(data) != "proxied" { t.Fatalf("unexpected response body %q", data) }
    if proxyHits.Load() != 1 { t.Fatalf("expected one proxy hit, got %d", proxyHits.Load()) }
    if directHits.Load() != 0 { t.Fatalf("direct sentinel was reached %d times", directHits.Load()) }

    if _, _, err = DownloadMedia(context.Background(), "image/jpeg", direct.URL, 1024); err == nil {
        t.Fatal("expected unscoped media request to fail closed")
    }
    if directHits.Load() != 0 { t.Fatalf("unscoped request reached direct sentinel %d times", directHits.Load()) }
}
''',
    )
    replace_exact(
        media_test,
        '"context"\n    "net/http"\n    "testing"',
        '"context"\n    "io"\n    "net/http"\n    "net/http/httptest"\n    "sync/atomic"\n    "testing"',
        1,
    )

    print(f"Applied Phase 3 hardening fixes to {root}")


if __name__ == "__main__":
    main()
