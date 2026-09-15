#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import sys

UPSTREAM_SHA = "ed37c9e6ce47e83dc75b9abea7b636302715b9bc"


def replace_once(path: pathlib.Path, old: str, new: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one upstream fragment, found {count}")
    path.write_text(text.replace(old, new, 1))


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_patch.py <mautrix-meta-source-dir>")
    root = pathlib.Path(sys.argv[1]).resolve()
    if not (root / "go.mod").is_file():
        raise SystemExit(f"not a mautrix-meta source tree: {root}")

    client = root / "pkg/connector/client.go"
    login = root / "pkg/connector/login.go"
    directmedia = root / "pkg/connector/directmedia.go"
    events = root / "pkg/connector/events.go"
    userinfo = root / "pkg/connector/userinfo.go"
    contextkey = root / "pkg/msgconv/mediadl/contextkey.go"
    download = root / "pkg/msgconv/mediadl/download.go"

    replace_once(
        client,
        '"net/http"\n\t"net/url"\n\t"sync"',
        '"net/http"\n\t"net/url"\n\t"os"\n\t"sync"',
    )

    replace_once(
        client,
        '''type respGetProxy struct {\n\tProxyURL string `json:"proxy_url"`\n}\n\n// TODO this should be moved into mautrix-go\n\nfunc (m *MetaConnector) getProxy(reason string) (string, error) {\n\tif m.Config.GetProxyFrom == "" {\n\t\treturn m.Config.Proxy, nil\n\t}\n\tparsed, err := url.Parse(m.Config.GetProxyFrom)\n\tif err != nil {\n\t\treturn "", fmt.Errorf("failed to parse address: %w", err)\n\t}\n\tq := parsed.Query()\n\tq.Set("reason", reason)\n\tparsed.RawQuery = q.Encode()\n\treq, err := http.NewRequest(http.MethodGet, parsed.String(), nil)\n\tif err != nil {\n\t\treturn "", fmt.Errorf("failed to prepare request: %w", err)\n\t}\n\treq.Header.Set("User-Agent", mautrix.DefaultUserAgent)\n\tresp, err := http.DefaultClient.Do(req)\n\tif err != nil {\n\t\treturn "", fmt.Errorf("failed to send request: %w", err)\n\t} else if resp.StatusCode >= 300 || resp.StatusCode < 200 {\n\t\treturn "", fmt.Errorf("unexpected status code %d", resp.StatusCode)\n\t}\n\tvar respData respGetProxy\n\terr = json.NewDecoder(resp.Body).Decode(&respData)\n\tif err != nil {\n\t\treturn "", fmt.Errorf("failed to decode response: %w", err)\n\t}\n\treturn respData.ProxyURL, nil\n}\n''',
        '''type ProxyTrafficClass string\n\nconst (\n\tProxyTrafficLogin     ProxyTrafficClass = "login"\n\tProxyTrafficMessaging ProxyTrafficClass = "messaging"\n\tProxyTrafficMedia     ProxyTrafficClass = "media"\n\tProxyTrafficE2EE      ProxyTrafficClass = "e2ee"\n)\n\ntype ProxyContext struct {\n\tMetaAccountID string\n\tLoginID       string\n\tTrafficClass  ProxyTrafficClass\n}\n\ntype respGetProxy struct {\n\tProxyURL string `json:"proxy_url"`\n}\n\n// TODO this should be moved into mautrix-go\n\nfunc (m *MetaConnector) getProxy(proxyCtx ProxyContext) func(string) (string, error) {\n\treturn func(reason string) (string, error) {\n\t\tif m.Config.GetProxyFrom == "" {\n\t\t\treturn m.Config.Proxy, nil\n\t\t}\n\t\tif proxyCtx.MetaAccountID == "" && proxyCtx.LoginID == "" {\n\t\t\treturn "", fmt.Errorf("proxy resolver requires stable account or login identity")\n\t\t}\n\t\tif proxyCtx.TrafficClass == "" {\n\t\t\treturn "", fmt.Errorf("proxy resolver requires traffic class")\n\t\t}\n\t\ttoken := os.Getenv("MAUTRIX_META_EGRESS_TOKEN")\n\t\tif token == "" {\n\t\t\treturn "", fmt.Errorf("proxy resolver token is not configured")\n\t\t}\n\t\tparsed, err := url.Parse(m.Config.GetProxyFrom)\n\t\tif err != nil {\n\t\t\treturn "", fmt.Errorf("failed to parse address: %w", err)\n\t\t}\n\t\tq := parsed.Query()\n\t\tif proxyCtx.MetaAccountID != "" {\n\t\t\tq.Set("meta_account_id", proxyCtx.MetaAccountID)\n\t\t}\n\t\tif proxyCtx.LoginID != "" {\n\t\t\tq.Set("login_id", proxyCtx.LoginID)\n\t\t}\n\t\tq.Set("reason", reason)\n\t\tq.Set("traffic_class", string(proxyCtx.TrafficClass))\n\t\tparsed.RawQuery = q.Encode()\n\t\treq, err := http.NewRequest(http.MethodGet, parsed.String(), nil)\n\t\tif err != nil {\n\t\t\treturn "", fmt.Errorf("failed to prepare request: %w", err)\n\t\t}\n\t\treq.Header.Set("User-Agent", mautrix.DefaultUserAgent)\n\t\treq.Header.Set("Authorization", "Bearer "+token)\n\t\tresp, err := http.DefaultClient.Do(req)\n\t\tif err != nil {\n\t\t\treturn "", fmt.Errorf("failed to send request: %w", err)\n\t\t}\n\t\tdefer resp.Body.Close()\n\t\tif resp.StatusCode >= 300 || resp.StatusCode < 200 {\n\t\t\treturn "", fmt.Errorf("unexpected status code %d", resp.StatusCode)\n\t\t}\n\t\tvar respData respGetProxy\n\t\terr = json.NewDecoder(resp.Body).Decode(&respData)\n\t\tif err != nil {\n\t\t\treturn "", fmt.Errorf("failed to decode response: %w", err)\n\t\t}\n\t\tproxyURL, err := url.Parse(respData.ProxyURL)\n\t\tif err != nil || proxyURL.Scheme == "" || proxyURL.Host == "" {\n\t\t\treturn "", fmt.Errorf("proxy resolver returned invalid proxy URL")\n\t\t}\n\t\treturn respData.ProxyURL, nil\n\t}\n}\n\nfunc (m *MetaClient) proxyContext(trafficClass ProxyTrafficClass) ProxyContext {\n\tmetaAccountID := ""\n\tif m.LoginMeta != nil && m.LoginMeta.Cookies != nil && m.LoginMeta.Cookies.GetUserID() != 0 {\n\t\tmetaAccountID = fmt.Sprint(m.LoginMeta.Cookies.GetUserID())\n\t}\n\treturn ProxyContext{\n\t\tMetaAccountID: metaAccountID,\n\t\tLoginID:       string(m.UserLogin.ID),\n\t\tTrafficClass:  trafficClass,\n\t}\n}\n''',
    )

    replace_once(
        client,
        '''\t\tcli.GetHTTP().GetNewProxy = m.Main.getProxy\n\t\tif !cli.GetHTTP().UpdateProxy("connect") {''',
        '''\t\tcli.GetHTTP().GetNewProxy = m.Main.getProxy(m.proxyContext(ProxyTrafficMessaging))\n\t\tif !cli.GetHTTP().UpdateProxy("connect") {''',
    )

    replace_once(
        client,
        '''\tif m.Main.Config.ProxyE2EE && m.Main.Config.Proxy != "" {\n\t\tm.E2EEClient.SetProxyAddress(m.Main.Config.Proxy)\n\t}\n''',
        '''\tif m.Main.Config.ProxyE2EE && (m.Main.Config.GetProxyFrom != "" || m.Main.Config.Proxy != "") {\n\t\tproxyURL, proxyErr := m.Main.getProxy(m.proxyContext(ProxyTrafficE2EE))("connect")\n\t\tif proxyErr != nil {\n\t\t\treturn fmt.Errorf("failed to resolve e2ee proxy: %w", proxyErr)\n\t\t} else if proxyURL == "" {\n\t\t\treturn fmt.Errorf("e2ee proxy resolution returned empty proxy")\n\t\t}\n\t\tm.E2EEClient.SetProxyAddress(proxyURL)\n\t}\n''',
    )

    replace_once(
        login,
        '''\tif useProxy && (conn.Config.GetProxyFrom != "" || conn.Config.Proxy != "") {\n\t\tclient.GetHTTP().GetNewProxy = conn.getProxy\n\t\tif !client.GetHTTP().UpdateProxy("login") {\n\t\t\treturn nil, fmt.Errorf("failed to update proxy")\n\t\t}\n\t}\n''',
        '''\tif useProxy && (conn.Config.GetProxyFrom != "" || conn.Config.Proxy != "") {\n\t\tmetaAccountID := c.GetUserID()\n\t\tif conn.Config.GetProxyFrom != "" && metaAccountID == 0 {\n\t\t\treturn nil, fmt.Errorf("stable account identity is required before proxy-enabled login")\n\t\t}\n\t\tclient.GetHTTP().GetNewProxy = conn.getProxy(ProxyContext{\n\t\t\tMetaAccountID: fmt.Sprint(metaAccountID),\n\t\t\tTrafficClass:  ProxyTrafficLogin,\n\t\t})\n\t\tif !client.GetHTTP().UpdateProxy("login") {\n\t\t\treturn nil, fmt.Errorf("failed to update proxy")\n\t\t}\n\t}\n''',
    )

    replace_once(
        contextkey,
        '''\tContextKeyPartID\n)\n''',
        '''\tContextKeyPartID\n\tContextKeyProxyURL\n)\n\nfunc WithProxy(ctx context.Context, proxyURL string) context.Context {\n\treturn context.WithValue(ctx, ContextKeyProxyURL, proxyURL)\n}\n''',
    )

    replace_once(
        download,
        '''var mediaHTTPClient *http.Client\nvar BypassOnionForMedia bool\n''',
        '''var mediaHTTPClient *http.Client\nvar BypassOnionForMedia bool\n\nfunc clientForContext(ctx context.Context) (*http.Client, error) {\n\tproxyURL, _ := ctx.Value(ContextKeyProxyURL).(string)\n\tif proxyURL == "" {\n\t\treturn mediaHTTPClient, nil\n\t}\n\tparsedURL, err := url.Parse(proxyURL)\n\tif err != nil || parsedURL.Scheme == "" || parsedURL.Host == "" {\n\t\treturn nil, fmt.Errorf("invalid media proxy URL")\n\t}\n\tbaseTransport, ok := mediaHTTPClient.Transport.(*http.Transport)\n\tif !ok {\n\t\treturn nil, fmt.Errorf("media HTTP transport does not support request-scoped proxy")\n\t}\n\ttransport := baseTransport.Clone()\n\ttransport.Proxy = http.ProxyURL(parsedURL)\n\treturn &http.Client{\n\t\tTransport:     transport,\n\t\tCheckRedirect: mediaHTTPClient.CheckRedirect,\n\t\tTimeout:       mediaHTTPClient.Timeout,\n\t}, nil\n}\n''',
    )

    replace_once(
        download,
        '''\tresp, err := mediaHTTPClient.Do(req)\n\tif err != nil {''',
        '''\trequestClient, err := clientForContext(ctx)\n\tif err != nil {\n\t\treturn 0, nil, err\n\t}\n\tresp, err := requestClient.Do(req)\n\tif err != nil {''',
    )

    replace_once(
        download,
        '''\tresp, err := mediaHTTPClient.Do(req)\n\tif err == nil {\n\t\t// There's no body so close it immediately''',
        '''\trequestClient, err := clientForContext(ctx)\n\tif err != nil {\n\t\treturn 0, nil, err\n\t}\n\tresp, err := requestClient.Do(req)\n\tif err == nil {\n\t\t// There's no body so close it immediately''',
    )

    replace_once(
        directmedia,
        '''\tzerolog.Ctx(ctx).Trace().Any("mediaInfo", mediaInfo).Any("err", err).Msg("download direct media")\n\n\tvar msg *database.Message\n''',
        '''\tzerolog.Ctx(ctx).Trace().Any("mediaInfo", mediaInfo).Any("err", err).Msg("download direct media")\n\n\tif m.Config.ProxyMedia && (m.Config.GetProxyFrom != "" || m.Config.Proxy != "") {\n\t\tul := m.Bridge.GetCachedUserLoginByID(mediaInfo.UserID)\n\t\tif ul == nil || !ul.Client.IsLoggedIn() {\n\t\t\treturn nil, fmt.Errorf("login %s not found for media proxy resolution", mediaInfo.UserID)\n\t\t}\n\t\tclient := ul.Client.(*MetaClient)\n\t\tproxyURL, proxyErr := m.getProxy(client.proxyContext(ProxyTrafficMedia))("download")\n\t\tif proxyErr != nil {\n\t\t\treturn nil, fmt.Errorf("failed to resolve media proxy: %w", proxyErr)\n\t\t} else if proxyURL == "" {\n\t\t\treturn nil, fmt.Errorf("media proxy resolution returned empty proxy")\n\t\t}\n\t\tctx = mediadl.WithProxy(ctx, proxyURL)\n\t}\n\n\tvar msg *database.Message\n''',
    )

    replace_once(
        events,
        '''\t"go.mau.fi/mautrix-meta/pkg/messagix/table"\n\t"go.mau.fi/mautrix-meta/pkg/metaid"\n)''',
        '''\t"go.mau.fi/mautrix-meta/pkg/messagix/table"\n\t"go.mau.fi/mautrix-meta/pkg/metaid"\n\t"go.mau.fi/mautrix-meta/pkg/msgconv/mediadl"\n)''',
    )

    replace_once(
        events,
        '''func (evt *FBMessageEvent) ConvertMessage(ctx context.Context, portal *bridgev2.Portal, intent bridgev2.MatrixAPI) (*bridgev2.ConvertedMessage, error) {\n\tcli := evt.m.Client\n\tif cli == nil {\n\t\treturn nil, messagix.ErrClientIsNil\n\t}\n\treturn evt.m.Main.MsgConv.ToMatrix(ctx, portal, cli, evt.m.UserLogin, intent, evt.GetID(), evt.WrappedMessage, evt.m.Main.Config.DisableXMAAlways), nil\n}\n''',
        '''func (evt *FBMessageEvent) ConvertMessage(ctx context.Context, portal *bridgev2.Portal, intent bridgev2.MatrixAPI) (*bridgev2.ConvertedMessage, error) {\n\tcli := evt.m.Client\n\tif cli == nil {\n\t\treturn nil, messagix.ErrClientIsNil\n\t}\n\tif evt.m.Main.Config.ProxyMedia && (evt.m.Main.Config.GetProxyFrom != "" || evt.m.Main.Config.Proxy != "") {\n\t\tproxyURL, err := evt.m.Main.getProxy(evt.m.proxyContext(ProxyTrafficMedia))("message")\n\t\tif err != nil {\n\t\t\treturn nil, fmt.Errorf("failed to resolve media proxy: %w", err)\n\t\t} else if proxyURL == "" {\n\t\t\treturn nil, fmt.Errorf("media proxy resolution returned empty proxy")\n\t\t}\n\t\tctx = mediadl.WithProxy(ctx, proxyURL)\n\t}\n\treturn evt.m.Main.MsgConv.ToMatrix(ctx, portal, cli, evt.m.UserLogin, intent, evt.GetID(), evt.WrappedMessage, evt.m.Main.Config.DisableXMAAlways), nil\n}\n''',
    )

    replace_once(
        userinfo,
        '''\t\tAvatar: wrapAvatar(info.GetAvatarURL()),''',
        '''\t\tAvatar: m.wrapAvatar(info.GetAvatarURL()),''',
    )

    replace_once(
        userinfo,
        '''func wrapAvatar(avatarURL string) *bridgev2.Avatar {\n\tif avatarURL == "" {\n\t\treturn &bridgev2.Avatar{Remove: true}\n\t}\n\tparsedURL, _ := url.Parse(avatarURL)\n\tavatarID := path.Base(parsedURL.Path)\n\treturn &bridgev2.Avatar{\n\t\tID: networkid.AvatarID(avatarID),\n\t\tGet: func(ctx context.Context) ([]byte, error) {\n\t\t\treturn mediadl.DownloadAvatar(ctx, avatarURL)\n\t\t},\n\t}\n}\n''',
        '''func (m *MetaClient) wrapAvatar(avatarURL string) *bridgev2.Avatar {\n\tif avatarURL == "" {\n\t\treturn &bridgev2.Avatar{Remove: true}\n\t}\n\tparsedURL, _ := url.Parse(avatarURL)\n\tavatarID := path.Base(parsedURL.Path)\n\treturn &bridgev2.Avatar{\n\t\tID: networkid.AvatarID(avatarID),\n\t\tGet: func(ctx context.Context) ([]byte, error) {\n\t\t\tif m.Main.Config.ProxyMedia && (m.Main.Config.GetProxyFrom != "" || m.Main.Config.Proxy != "") {\n\t\t\t\tproxyURL, err := m.Main.getProxy(m.proxyContext(ProxyTrafficMedia))("avatar")\n\t\t\t\tif err != nil {\n\t\t\t\t\treturn nil, fmt.Errorf("failed to resolve avatar proxy: %w", err)\n\t\t\t\t} else if proxyURL == "" {\n\t\t\t\t\treturn nil, fmt.Errorf("avatar proxy resolution returned empty proxy")\n\t\t\t\t}\n\t\t\t\tctx = mediadl.WithProxy(ctx, proxyURL)\n\t\t\t}\n\t\t\treturn mediadl.DownloadAvatar(ctx, avatarURL)\n\t\t},\n\t}\n}\n''',
    )

    test_file = root / "pkg/connector/proxy_context_test.go"
    if test_file.exists():
        raise SystemExit(f"unexpected existing file: {test_file}")
    test_file.write_text(r'''package connector

import (
    "encoding/json"
    "net/http"
    "net/http/httptest"
    "os"
    "testing"
)

func TestAccountAwareProxyResolverContextAndAuth(t *testing.T) {
    const token = "phase3-test-token-not-secret"
    old := os.Getenv("MAUTRIX_META_EGRESS_TOKEN")
    t.Cleanup(func() { _ = os.Setenv("MAUTRIX_META_EGRESS_TOKEN", old) })
    if err := os.Setenv("MAUTRIX_META_EGRESS_TOKEN", token); err != nil { t.Fatal(err) }

    server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        if got := r.Header.Get("Authorization"); got != "Bearer "+token { t.Fatalf("bad auth header: %q", got) }
        q := r.URL.Query()
        if q.Get("meta_account_id") != "123" { t.Fatalf("bad account id: %q", q.Get("meta_account_id")) }
        if q.Get("login_id") != "123" { t.Fatalf("bad login id: %q", q.Get("login_id")) }
        if q.Get("reason") != "reconnect" { t.Fatalf("bad reason: %q", q.Get("reason")) }
        if q.Get("traffic_class") != "messaging" { t.Fatalf("bad traffic class: %q", q.Get("traffic_class")) }
        _ = json.NewEncoder(w).Encode(respGetProxy{ProxyURL: "socks5://user:secret@proxy.test:1080"})
    }))
    defer server.Close()

    conn := &MetaConnector{}
    conn.Config.GetProxyFrom = server.URL
    resolve := conn.getProxy(ProxyContext{MetaAccountID: "123", LoginID: "123", TrafficClass: ProxyTrafficMessaging})
    got, err := resolve("reconnect")
    if err != nil { t.Fatal(err) }
    if got != "socks5://user:secret@proxy.test:1080" { t.Fatalf("unexpected proxy: %q", got) }
}

func TestDynamicProxyResolverFailsWithoutIdentityOrToken(t *testing.T) {
    conn := &MetaConnector{}
    conn.Config.GetProxyFrom = "http://127.0.0.1:1"
    if _, err := conn.getProxy(ProxyContext{TrafficClass: ProxyTrafficLogin})("login"); err == nil {
        t.Fatal("expected missing identity error")
    }
    old := os.Getenv("MAUTRIX_META_EGRESS_TOKEN")
    t.Cleanup(func() { _ = os.Setenv("MAUTRIX_META_EGRESS_TOKEN", old) })
    _ = os.Unsetenv("MAUTRIX_META_EGRESS_TOKEN")
    if _, err := conn.getProxy(ProxyContext{MetaAccountID: "123", TrafficClass: ProxyTrafficLogin})("login"); err == nil {
        t.Fatal("expected missing token error")
    }
}
''')

    media_test = root / "pkg/msgconv/mediadl/proxy_context_test.go"
    if media_test.exists():
        raise SystemExit(f"unexpected existing file: {media_test}")
    media_test.write_text(r'''package mediadl

import (
    "context"
    "net/http"
    "testing"
)

func TestClientForContextUsesRequestScopedTransport(t *testing.T) {
    mediaHTTPClient = &http.Client{Transport: http.DefaultTransport.(*http.Transport).Clone()}
    base := mediaHTTPClient.Transport.(*http.Transport)
    if base.Proxy != nil { t.Fatal("expected clean baseline proxy") }
    scoped, err := clientForContext(WithProxy(context.Background(), "socks5://user:secret@proxy.test:1080"))
    if err != nil { t.Fatal(err) }
    if scoped == mediaHTTPClient { t.Fatal("expected request-scoped client") }
    if mediaHTTPClient.Transport.(*http.Transport).Proxy != nil { t.Fatal("global transport was mutated") }
    if scoped.Transport.(*http.Transport).Proxy == nil { t.Fatal("scoped transport proxy missing") }
}
''')

    print(f"Applied Phase 3 account-aware egress delta to {root}")


if __name__ == "__main__":
    main()
