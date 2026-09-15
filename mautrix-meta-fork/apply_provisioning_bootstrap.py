#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import re
import sys


def replace_once(path: pathlib.Path, old: str, new: str) -> None:
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one occurrence of {old!r}, found {count}")
    path.write_text(text.replace(old, new, 1))


def replace_regex_once(path: pathlib.Path, pattern: str, replacement: str) -> None:
    text = path.read_text()
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one regex match for {pattern!r}, found {count}")
    path.write_text(updated)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_provisioning_bootstrap.py <patched-mautrix-meta-source-dir>")
    root = pathlib.Path(sys.argv[1]).resolve()
    login = root / "pkg/connector/login.go"
    provisioning = root / "pkg/connector/provisioning.go"
    provisioning_test = root / "pkg/connector/provisioning_test.go"

    if provisioning.exists() or provisioning_test.exists():
        raise SystemExit("provisioning bootstrap files already exist")

    replace_once(
        login,
        '''type MetaCookieLogin struct {\n\tMode types.Platform\n\tUser *bridgev2.User\n\tMain *MetaConnector\n}\n''',
        '''type MetaCookieLogin struct {\n\tMode types.Platform\n\tUser *bridgev2.User\n\tMain *MetaConnector\n\n\tProvisioningClaim        string\n\tProvisioningConnectionID string\n}\n''',
    )

    replace_regex_once(
        login,
        r'''func \(m \*MetaCookieLogin\) Start\(ctx context\.Context\) \(\*bridgev2\.LoginStep, error\) \{.*?\n\treturn step, nil\n\}\n\nfunc \(m \*MetaCookieLogin\) Cancel\(\) \{\}''',
        '''func (m *MetaCookieLogin) cookieStep() (*bridgev2.LoginStep, error) {\n\tstep := &bridgev2.LoginStep{\n\t\tType:         bridgev2.LoginStepTypeCookies,\n\t\tStepID:       LoginStepIDCookies,\n\t\tInstructions: "Enter a JSON object with your cookies, or a cURL command copied from browser devtools.",\n\t\tCookiesParams: &bridgev2.LoginCookiesParams{\n\t\t\tUserAgent: useragent.UserAgent,\n\t\t},\n\t}\n\tswitch m.Mode {\n\tcase types.Facebook, types.FacebookTor:\n\t\tstep.CookiesParams.URL = "https://www.facebook.com/"\n\t\tstep.CookiesParams.Fields = cookieListToFields(cookies.FBRequiredCookies, "facebook.com")\n\t\tstep.CookiesParams.WaitForURLPattern = "^https://www\\\\.facebook\\\\.com/(?:messages/(?:e2ee/)?(?:t/[0-9]+/?)?)?(?:\\\\?.*)?$"\n\tcase types.Messenger:\n\t\tstep.CookiesParams.URL = "https://www.messenger.com/?no_redirect=true"\n\t\tstep.CookiesParams.Fields = cookieListToFields(cookies.FBRequiredCookies, "messenger.com")\n\t\tstep.CookiesParams.WaitForURLPattern = "^https://www\\\\.messenger\\\\.com/(?:e2ee/)?(?:t/[0-9]+/?)?(?:\\\\?.*)?$"\n\tcase types.Instagram:\n\t\tstep.CookiesParams.URL = "https://www.instagram.com/accounts/login/"\n\t\tstep.CookiesParams.Fields = cookieListToFields(cookies.IGRequiredCookies, "instagram.com")\n\t\tstep.CookiesParams.WaitForURLPattern = "^https://www\\\\.instagram\\\\.com/(?:direct/(?:inbox/|t/[0-9]+/)?)?(?:\\\\?.*)?$"\n\tdefault:\n\t\treturn nil, fmt.Errorf("unknown mode %s", m.Mode)\n\t}\n\treturn step, nil\n}\n\nfunc (m *MetaCookieLogin) Start(ctx context.Context) (*bridgev2.LoginStep, error) {\n\tif m.Main.Config.GetProxyFrom == "" {\n\t\treturn m.cookieStep()\n\t}\n\treturn &bridgev2.LoginStep{\n\t\tType:         bridgev2.LoginStepTypeUserInput,\n\t\tStepID:       "fi.mau.meta.provisioning_claim",\n\t\tInstructions: "Enter the one-time provisioning claim issued for this Meta connection.",\n\t\tUserInputParams: &bridgev2.LoginUserInputParams{\n\t\t\tFields: []bridgev2.LoginInputDataField{{\n\t\t\t\tType:        bridgev2.LoginInputFieldTypeToken,\n\t\t\t\tID:          "provisioning_claim",\n\t\t\t\tName:        "Provisioning claim",\n\t\t\t\tDescription: "Short-lived one-time claim for the pre-created Meta connection.",\n\t\t\t}},\n\t\t},\n\t}, nil\n}\n\nfunc (m *MetaCookieLogin) SubmitUserInput(ctx context.Context, input map[string]string) (*bridgev2.LoginStep, error) {\n\tif m.Main.Config.GetProxyFrom == "" {\n\t\treturn nil, fmt.Errorf("provisioning claim input is only valid with dynamic egress")\n\t}\n\tclaim := input["provisioning_claim"]\n\tif len(claim) < 40 || len(claim) > 512 {\n\t\treturn nil, fmt.Errorf("invalid provisioning claim")\n\t}\n\tm.ProvisioningClaim = claim\n\treturn m.cookieStep()\n}\n\nfunc (m *MetaCookieLogin) Cancel() {}''',
    )

    replace_once(
        login,
        '''func loginWithCookies(\n\tctx context.Context,\n\tlog zerolog.Logger,\n\tclient *messagix.Client,\n\tbridgeUser *bridgev2.User,\n\tconn *MetaConnector,\n\tc *cookies.Cookies,\n) (*bridgev2.LoginStep, error) {\n''',
        '''func loginWithCookies(\n\tctx context.Context,\n\tlog zerolog.Logger,\n\tclient *messagix.Client,\n\tbridgeUser *bridgev2.User,\n\tconn *MetaConnector,\n\tc *cookies.Cookies,\n\tprovisioningConnectionID string,\n) (*bridgev2.LoginStep, error) {\n''',
    )

    replace_once(
        login,
        '''\tloginID := metaid.MakeUserLoginID(id)\n\tif !conn.updateMessagingProxy(client, ProxyContext{\n''',
        '''\tloginID := metaid.MakeUserLoginID(id)\n\tif provisioningConnectionID != "" {\n\t\tif err = conn.bindProvisionedLogin(ctx, provisioningConnectionID, fmt.Sprint(c.GetUserID()), string(loginID), bridgeUser.MXID.String()); err != nil {\n\t\t\treturn nil, fmt.Errorf("failed to bind provisioned login identity: %w", err)\n\t\t}\n\t}\n\tif !conn.updateMessagingProxy(client, ProxyContext{\n''',
    )

    replace_once(
        login,
        '''\tlog := m.User.Log.With().Str("component", "messagix").Logger()\n\tclient, err := getMessagixClient(log, m.Main, c, m.Main.Config.ProxyOther)\n\tif err != nil {\n\t\treturn nil, err\n\t}\n\treturn loginWithCookies(ctx, log, client, m.User, m.Main, c)\n''',
        '''\tlog := m.User.Log.With().Str("component", "messagix").Logger()\n\tvar client *messagix.Client\n\tvar err error\n\tif m.Main.Config.GetProxyFrom != "" {\n\t\tif m.ProvisioningClaim == "" {\n\t\t\treturn nil, fmt.Errorf("provisioning claim is required before cookies")\n\t\t}\n\t\tmetaAccountID := c.GetUserID()\n\t\tif metaAccountID == 0 {\n\t\t\treturn nil, fmt.Errorf("stable account identity is required before provisioned login")\n\t\t}\n\t\tbootstrap, consumeErr := m.Main.consumeProvisioningClaim(ctx, m.ProvisioningClaim, fmt.Sprint(metaAccountID), m.User.MXID.String())\n\t\tif consumeErr != nil {\n\t\t\treturn nil, consumeErr\n\t\t}\n\t\tm.ProvisioningConnectionID = bootstrap.ConnectionID\n\t\tclient = messagix.NewClient(c, log, m.Main.getMessagixConfig())\n\t\tif err = configureBootstrapLoginProxy(client, bootstrap.ProxyURL); err != nil {\n\t\t\treturn nil, err\n\t\t}\n\t} else {\n\t\tclient, err = getMessagixClient(log, m.Main, c, m.Main.Config.ProxyOther)\n\t\tif err != nil {\n\t\t\treturn nil, err\n\t\t}\n\t}\n\treturn loginWithCookies(ctx, log, client, m.User, m.Main, c, m.ProvisioningConnectionID)\n''',
    )

    provisioning.write_text(r'''package connector

import (
    "bytes"
    "context"
    "encoding/json"
    "fmt"
    "io"
    "net/http"
    "net/url"
    "os"

    "maunium.net/go/mautrix"

    "go.mau.fi/mautrix-meta/pkg/messagix"
)

const maxProvisioningResponseBytes = 64 * 1024

type provisioningConsumeRequest struct {
    Claim           string `json:"claim"`
    MetaAccountID   string `json:"metaAccountId"`
    MatrixOwnerMXID string `json:"matrixOwnerMxid"`
}

type provisioningConsumeResponse struct {
    ConnectionID string `json:"connection_id"`
    TenantID     string `json:"tenant_id"`
    MetaAccountID string `json:"meta_account_id"`
    ProxyURL     string `json:"proxy_url"`
    AssignmentID string `json:"assignment_id"`
}

type provisioningBindLoginRequest struct {
    ConnectionID   string `json:"connectionId"`
    MetaAccountID  string `json:"metaAccountId"`
    LoginID        string `json:"loginId"`
    MatrixOwnerMXID string `json:"matrixOwnerMxid"`
}

func (m *MetaConnector) provisioningEndpoint(path string) (string, error) {
    endpoint, err := url.Parse(m.Config.GetProxyFrom)
    if err != nil || endpoint.Scheme == "" || endpoint.Host == "" {
        return "", fmt.Errorf("invalid control-plane resolver URL")
    }
    endpoint.Path = path
    endpoint.RawPath = ""
    endpoint.RawQuery = ""
    endpoint.Fragment = ""
    return endpoint.String(), nil
}

func (m *MetaConnector) provisioningRequest(ctx context.Context, path string, input any, output any) error {
    endpoint, err := m.provisioningEndpoint(path)
    if err != nil {
        return err
    }
    token := os.Getenv("MAUTRIX_META_EGRESS_TOKEN")
    if token == "" {
        return fmt.Errorf("MAUTRIX_META_EGRESS_TOKEN is required for provisioning")
    }
    body, err := json.Marshal(input)
    if err != nil {
        return fmt.Errorf("failed to encode provisioning request: %w", err)
    }
    req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
    if err != nil {
        return fmt.Errorf("failed to prepare provisioning request: %w", err)
    }
    req.Header.Set("Authorization", "Bearer "+token)
    req.Header.Set("Content-Type", "application/json")
    req.Header.Set("User-Agent", mautrix.DefaultUserAgent)
    resp, err := proxyResolverHTTPClient.Do(req)
    if err != nil {
        return fmt.Errorf("failed to send provisioning request: %w", err)
    }
    defer resp.Body.Close()
    if resp.StatusCode < 200 || resp.StatusCode >= 300 {
        return fmt.Errorf("provisioning request failed with status %d", resp.StatusCode)
    }
    raw, err := io.ReadAll(io.LimitReader(resp.Body, maxProvisioningResponseBytes+1))
    if err != nil {
        return fmt.Errorf("failed to read provisioning response: %w", err)
    }
    if len(raw) > maxProvisioningResponseBytes {
        return fmt.Errorf("provisioning response exceeds size limit")
    }
    if err = json.Unmarshal(raw, output); err != nil {
        return fmt.Errorf("failed to decode provisioning response: %w", err)
    }
    return nil
}

func (m *MetaConnector) consumeProvisioningClaim(ctx context.Context, claim, metaAccountID, matrixOwnerMXID string) (*provisioningConsumeResponse, error) {
    var output provisioningConsumeResponse
    err := m.provisioningRequest(ctx, "/internal/v1/provisioning/consume", provisioningConsumeRequest{
        Claim: claim, MetaAccountID: metaAccountID, MatrixOwnerMXID: matrixOwnerMXID,
    }, &output)
    if err != nil {
        return nil, err
    }
    if output.ConnectionID == "" || output.MetaAccountID != metaAccountID || output.ProxyURL == "" || output.AssignmentID == "" {
        return nil, fmt.Errorf("invalid provisioning consume response")
    }
    parsed, err := url.Parse(output.ProxyURL)
    if err != nil || parsed.Hostname() == "" || parsed.Port() == "" || parsed.Path != "" || parsed.RawQuery != "" || parsed.Fragment != "" {
        return nil, fmt.Errorf("provisioning response returned invalid proxy URL")
    }
    switch parsed.Scheme {
    case "http", "https", "socks5":
    default:
        return nil, fmt.Errorf("provisioning response returned unsupported proxy scheme")
    }
    return &output, nil
}

func (m *MetaConnector) bindProvisionedLogin(ctx context.Context, connectionID, metaAccountID, loginID, matrixOwnerMXID string) error {
    var output map[string]any
    return m.provisioningRequest(ctx, "/internal/v1/provisioning/bind-login", provisioningBindLoginRequest{
        ConnectionID: connectionID, MetaAccountID: metaAccountID, LoginID: loginID, MatrixOwnerMXID: matrixOwnerMXID,
    }, &output)
}

func configureBootstrapLoginProxy(client *messagix.Client, proxyURL string) error {
    if client == nil || proxyURL == "" {
        return fmt.Errorf("bootstrap proxy is required")
    }
    client.GetHTTP().GetNewProxy = func(string) (string, error) { return proxyURL, nil }
    if !client.GetHTTP().UpdateProxy("provisioning-login") {
        return fmt.Errorf("failed to configure bootstrap login proxy")
    }
    return nil
}
''')

    provisioning_test.write_text(r'''package connector

import (
    "context"
    "encoding/json"
    "net/http"
    "net/http/httptest"
    "os"
    "sync/atomic"
    "testing"

    "maunium.net/go/mautrix/bridgev2"

    "go.mau.fi/mautrix-meta/pkg/messagix/cookies"
    "go.mau.fi/mautrix-meta/pkg/messagix/types"
)

func TestDynamicLoginRequiresProvisioningClaimBeforeCookies(t *testing.T) {
    login := &MetaCookieLogin{Mode: types.Facebook, Main: &MetaConnector{Config: Config{GetProxyFrom: "http://control-plane/internal/v1/egress/resolve"}}}
    first, err := login.Start(context.Background())
    if err != nil { t.Fatal(err) }
    if first.Type != bridgev2.LoginStepTypeUserInput || first.StepID != "fi.mau.meta.provisioning_claim" {
        t.Fatalf("unexpected first step: %#v", first)
    }
    second, err := login.SubmitUserInput(context.Background(), map[string]string{"provisioning_claim": "pc_00000000-0000-0000-0000-000000000000.abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN"})
    if err != nil { t.Fatal(err) }
    if second.Type != bridgev2.LoginStepTypeCookies {
        t.Fatalf("expected cookies only after provisioning claim, got %s", second.Type)
    }
}

func TestProvisioningConsumeUsesPrivateClientAndBootstrapProxy(t *testing.T) {
    const token = "provisioning-internal-token"
    oldToken := os.Getenv("MAUTRIX_META_EGRESS_TOKEN")
    t.Cleanup(func() { _ = os.Setenv("MAUTRIX_META_EGRESS_TOKEN", oldToken) })
    if err := os.Setenv("MAUTRIX_META_EGRESS_TOKEN", token); err != nil { t.Fatal(err) }

    var directHits atomic.Int32
    direct := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        directHits.Add(1)
        _, _ = w.Write([]byte("DIRECT"))
    }))
    defer direct.Close()

    var proxyHits atomic.Int32
    proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        proxyHits.Add(1)
        _, _ = w.Write([]byte("PROXIED"))
    }))
    defer proxy.Close()

    var consumeHits atomic.Int32
    controlPlane := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        if r.URL.Path != "/internal/v1/provisioning/consume" { t.Fatalf("unexpected path %s", r.URL.Path) }
        if r.Header.Get("Authorization") != "Bearer "+token { t.Fatal("missing internal bearer token") }
        consumeHits.Add(1)
        var body provisioningConsumeRequest
        if err := json.NewDecoder(r.Body).Decode(&body); err != nil { t.Fatal(err) }
        if body.MetaAccountID != "123" || body.MatrixOwnerMXID != "@owner:test" { t.Fatalf("wrong identity: %#v", body) }
        _ = json.NewEncoder(w).Encode(provisioningConsumeResponse{
            ConnectionID: "conn-1", TenantID: "tenant-1", MetaAccountID: "123", ProxyURL: proxy.URL, AssignmentID: "proxy-1",
        })
    }))
    defer controlPlane.Close()

    conn := &MetaConnector{Config: Config{GetProxyFrom: controlPlane.URL + "/internal/v1/egress/resolve"}}
    bootstrap, err := conn.consumeProvisioningClaim(context.Background(), "pc_claim.secret", "123", "@owner:test")
    if err != nil { t.Fatal(err) }
    if consumeHits.Load() != 1 { t.Fatalf("expected one claim consume, got %d", consumeHits.Load()) }

    c := &cookies.Cookies{Platform: types.Facebook}
    c.UpdateValues(map[cookies.MetaCookieName]string{cookies.FBCookieCUser: "123"})
    client := makePhase3TestClient(c)
    if err = configureBootstrapLoginProxy(client, bootstrap.ProxyURL); err != nil { t.Fatal(err) }
    resp, err := client.GetHTTP().HTTP.Get(direct.URL)
    if err != nil { t.Fatal(err) }
    _ = resp.Body.Close()
    if proxyHits.Load() != 1 { t.Fatalf("expected bootstrap proxy hit, got %d", proxyHits.Load()) }
    if directHits.Load() != 0 { t.Fatalf("direct provider sentinel was reached %d times", directHits.Load()) }
}

func TestProvisioningConsumeFailurePreventsAnyProviderTransport(t *testing.T) {
    const token = "provisioning-failure-token"
    oldToken := os.Getenv("MAUTRIX_META_EGRESS_TOKEN")
    t.Cleanup(func() { _ = os.Setenv("MAUTRIX_META_EGRESS_TOKEN", oldToken) })
    if err := os.Setenv("MAUTRIX_META_EGRESS_TOKEN", token); err != nil { t.Fatal(err) }

    var directHits atomic.Int32
    direct := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { directHits.Add(1) }))
    defer direct.Close()
    controlPlane := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Error(w, "denied", http.StatusConflict) }))
    defer controlPlane.Close()

    conn := &MetaConnector{Config: Config{GetProxyFrom: controlPlane.URL + "/internal/v1/egress/resolve"}}
    if _, err := conn.consumeProvisioningClaim(context.Background(), "pc_claim.secret", "123", "@owner:test"); err == nil {
        t.Fatal("expected failed provisioning claim to stop login")
    }
    if directHits.Load() != 0 { t.Fatalf("direct provider sentinel was reached %d times", directHits.Load()) }
}

func TestBindProvisionedLoginCarriesExactConnectionAndIdentity(t *testing.T) {
    const token = "provisioning-bind-token"
    oldToken := os.Getenv("MAUTRIX_META_EGRESS_TOKEN")
    t.Cleanup(func() { _ = os.Setenv("MAUTRIX_META_EGRESS_TOKEN", oldToken) })
    if err := os.Setenv("MAUTRIX_META_EGRESS_TOKEN", token); err != nil { t.Fatal(err) }

    controlPlane := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        if r.URL.Path != "/internal/v1/provisioning/bind-login" { t.Fatalf("unexpected path %s", r.URL.Path) }
        var body provisioningBindLoginRequest
        if err := json.NewDecoder(r.Body).Decode(&body); err != nil { t.Fatal(err) }
        if body.ConnectionID != "conn-1" || body.MetaAccountID != "123" || body.LoginID != "123" || body.MatrixOwnerMXID != "@owner:test" {
            t.Fatalf("wrong bind identity: %#v", body)
        }
        _ = json.NewEncoder(w).Encode(map[string]any{"ok": true})
    }))
    defer controlPlane.Close()

    conn := &MetaConnector{Config: Config{GetProxyFrom: controlPlane.URL + "/internal/v1/egress/resolve"}}
    if err := conn.bindProvisionedLogin(context.Background(), "conn-1", "123", "123", "@owner:test"); err != nil { t.Fatal(err) }
}
''')

    print(f"Applied provisioning bootstrap to {root}")


if __name__ == "__main__":
    main()
