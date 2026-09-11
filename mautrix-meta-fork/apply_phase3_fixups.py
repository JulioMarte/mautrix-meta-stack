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

    # wrapAvatar became account-aware in the first Phase 3 patch. All MetaClient
    # call sites must use that receiver; leaving any global call would either fail
    # to compile or reintroduce unscoped media egress.
    replace_exact(root / "pkg/connector/chatinfo.go", "wrapAvatar(", "m.wrapAvatar(", 2)
    replace_exact(root / "pkg/connector/handlemeta.go", "wrapAvatar(evt.ImageURL)", "m.wrapAvatar(evt.ImageURL)", 1)

    # http.DefaultTransport has ProxyFromEnvironment by default. The isolation
    # test needs an intentionally clean baseline so it can prove our helper does
    # not mutate the process-global transport.
    replace_exact(
        root / "pkg/msgconv/mediadl/proxy_context_test.go",
        "mediaHTTPClient = &http.Client{Transport: http.DefaultTransport.(*http.Transport).Clone()}",
        "mediaHTTPClient = &http.Client{Transport: &http.Transport{}}",
        1,
    )

    client = root / "pkg/connector/client.go"

    # Resolver calls are on the critical path for login, reconnect, media and
    # E2EE. Never use http.DefaultClient here: it has no overall request timeout.
    replace_exact(
        client,
        'type respGetProxy struct {\n\tProxyURL string `json:"proxy_url"`\n}\n',
        'type respGetProxy struct {\n\tProxyURL string `json:"proxy_url"`\n}\n\nvar proxyResolverHTTPClient = &http.Client{Timeout: 5 * time.Second}\n',
        1,
    )
    replace_exact(client, "resp, err := http.DefaultClient.Do(req)", "resp, err := proxyResolverHTTPClient.Do(req)", 1)

    # Cached reconnect is an upstream fast path that returns before the normal
    # UpdateProxy block. Resolve the account-bound messaging proxy before that
    # path can call Client.Connect(), otherwise a restart could bypass egress.
    replace_exact(
        client,
        '''\t\t} else {\n\t\t\tzerolog.Ctx(ctx).Debug().\n\t\t\t\tTime("last_used", lastUsed).\n\t\t\t\tMsg("Reconnecting with cached state")\n\t\t\tm.connectWithCache(ctx)\n\t\t\treturn\n\t\t}\n''',
        '''\t\t} else {\n\t\t\tzerolog.Ctx(ctx).Debug().\n\t\t\t\tTime("last_used", lastUsed).\n\t\t\t\tMsg("Reconnecting with cached state")\n\t\t\tif m.Main.Config.ProxyOther && (m.Main.Config.GetProxyFrom != "" || m.Main.Config.Proxy != "") {\n\t\t\t\tcli.GetHTTP().GetNewProxy = m.Main.getProxy(m.proxyContext(ProxyTrafficMessaging))\n\t\t\t\tif !cli.GetHTTP().UpdateProxy("reconnect-cache") {\n\t\t\t\t\tm.UserLogin.BridgeState.Send(status.BridgeState{\n\t\t\t\t\t\tStateEvent: status.StateUnknownError,\n\t\t\t\t\t\tError:      MetaProxyUpdateFail,\n\t\t\t\t\t})\n\t\t\t\t\treturn\n\t\t\t\t}\n\t\t\t}\n\t\t\tm.connectWithCache(ctx)\n\t\t\treturn\n\t\t}\n''',
        1,
    )

    # Dynamic account-aware egress is a security policy, not four independent
    # best-effort feature flags. Reject configurations that expose any protected
    # path as direct. Messenger Lite has no stable identity before its first
    # network request on this upstream baseline, so it is explicitly unsupported
    # while dynamic resolution is enabled.
    config = root / "pkg/connector/config.go"
    replace_exact(
        config,
        '''func (m *MetaConnector) ValidateConfig() error {\n\tif m.Config.Mode == types.Unset && m.Config.RawMode != "" {\n\t\treturn fmt.Errorf("invalid mode %q", m.Config.RawMode)\n\t}\n\treturn nil\n}\n''',
        '''func (m *MetaConnector) ValidateConfig() error {\n\tif m.Config.Mode == types.Unset && m.Config.RawMode != "" {\n\t\treturn fmt.Errorf("invalid mode %q", m.Config.RawMode)\n\t}\n\tif m.Config.GetProxyFrom != "" {\n\t\tif !m.Config.ProxyOther || !m.Config.ProxyMedia || !m.Config.ProxyE2EE {\n\t\t\treturn fmt.Errorf("dynamic egress requires proxy_other, proxy_media and proxy_e2ee")\n\t\t}\n\t\tif m.Config.ProxyMessengerLite {\n\t\t\treturn fmt.Errorf("dynamic egress does not support proxy_messenger_lite before stable login identity exists")\n\t\t}\n\t\tif len(m.Config.AllowedModes) == 0 && m.Config.Mode == types.Unset {\n\t\t\treturn fmt.Errorf("dynamic egress requires an explicit mode or allowed_modes that excludes messenger-lite")\n\t\t}\n\t\tif m.Config.Mode == types.MessengerLite {\n\t\t\treturn fmt.Errorf("dynamic egress does not support messenger-lite login")\n\t\t}\n\t\tfor _, mode := range m.Config.AllowedModes {\n\t\t\tif mode == types.MessengerLite {\n\t\t\t\treturn fmt.Errorf("dynamic egress allowed_modes cannot include messenger-lite")\n\t\t\t}\n\t\t}\n\t}\n\treturn nil\n}\n''',
        1,
    )

    connector_test = root / "pkg/connector/proxy_context_test.go"
    append_once(
        connector_test,
        "TestDynamicProxyResolverFailsWithoutIdentityOrToken",
        r'''

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

func TestDynamicEgressConfigRejectsPartialProxyCoverage(t *testing.T) {
    conn := &MetaConnector{}
    conn.Config.RawMode = "facebook"
    conn.Config.Mode = types.Facebook
    conn.Config.GetProxyFrom = "http://control-plane/internal/v1/egress/resolve"
    conn.Config.ProxyOther = true
    conn.Config.ProxyMedia = true
    conn.Config.ProxyE2EE = false
    if err := conn.ValidateConfig(); err == nil {
        t.Fatal("expected partial dynamic egress configuration to be rejected")
    }
}

func TestDynamicEgressConfigRejectsMessengerLite(t *testing.T) {
    conn := &MetaConnector{}
    conn.Config.RawMode = "messenger-lite"
    conn.Config.Mode = types.MessengerLite
    conn.Config.GetProxyFrom = "http://control-plane/internal/v1/egress/resolve"
    conn.Config.ProxyOther = true
    conn.Config.ProxyMedia = true
    conn.Config.ProxyE2EE = true
    if err := conn.ValidateConfig(); err == nil {
        t.Fatal("expected messenger-lite with dynamic egress to be rejected")
    }
}
''',
    )

    # The appended config tests need the platform enum and timeout test needs time.
    replace_exact(
        connector_test,
        '"os"\n    "testing"',
        '"os"\n    "testing"\n    "time"\n\n    "go.mau.fi/mautrix-meta/pkg/messagix/types"',
        1,
    )

    print(f"Applied Phase 3 hardening fixes to {root}")


if __name__ == "__main__":
    main()
