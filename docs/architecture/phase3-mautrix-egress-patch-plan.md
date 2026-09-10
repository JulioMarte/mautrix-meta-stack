# Phase 3 — mautrix-meta account-aware egress patch

Status: implementation contract
Base stack branch: `dev@f8ab8448ce11a6d86c73a3cbbf52de7e84a714ae`
Upstream mautrix baseline: `mautrix/meta@ed37c9e6ce47e83dc75b9abea7b636302715b9bc` (`v0.2607.0`)
Integration target: `dev`

## Goal

Make one mautrix-meta process resolve egress using stable account/login identity, without broad protocol refactoring and without allowing a `proxy_required` connection to fall back to host egress.

## Patch shape

The stack repository does not vendor the whole mautrix source tree. Phase 3 stores a deterministic patch applicator under `mautrix-meta-fork/`. CI clones the exact upstream commit, applies replacements that assert their expected upstream source text, runs the upstream Go test suite, and builds the patched image.

If an upstream source fragment no longer matches, patch application fails. That is intentional: an upstream upgrade must be reviewed rather than silently receiving a fuzzy patch.

## Resolver context

The fork introduces a small connector-level context:

```go
type ProxyContext struct {
    MetaAccountID string
    LoginID       string
    TrafficClass  ProxyTrafficClass
}
```

The existing callback reason remains dynamic because messagix may request a fresh proxy for connect, reconnect, or HTTP failure. Identity and traffic class are captured by the callback closure; the runtime `reason` remains an argument.

Resolver calls use the existing `network.get_proxy_from` URL but add:

- `meta_account_id`
- `login_id` when known
- `reason`
- `traffic_class`

Authentication comes from `MAUTRIX_META_EGRESS_TOKEN` and is sent as `Authorization: Bearer ...`. If `get_proxy_from` is configured and the token or stable identity is missing, resolution fails before a Meta-bound request.

## Four traffic paths

### 1. First login

Cookie login already has a stable account ID through `Cookies.GetUserID()` (`c_user` for Facebook/Messenger and `ds_user_id` for Instagram). The patch binds a `login` resolver callback before `UpdateProxy("login")` and before `LoadMessagesPage`.

A proxy-enabled native login flow that does not yet expose a stable account ID must fail closed rather than borrow a connector-global identity.

### 2. Messaging/connect/reconnect

An established `MetaClient` has both `UserLogin.ID` and persisted cookies. Its resolver callback includes both identifiers and uses traffic class `messaging`. Re-resolution changes the reason, not the account assignment.

### 3. E2EE

The upstream E2EE path currently calls `SetProxyAddress` with the connector-global static proxy. The patch resolves the same account-specific egress immediately before connecting the E2EE client and fails the E2EE connection if resolution fails.

### 4. Media

The upstream media downloader owns a process-global `http.Client`; mutating its transport proxy per account would race under concurrent tenants. Phase 3 therefore adds a request-context proxy override: a download clones the baseline transport and applies the account-specific proxy only to that request chain. The connector resolves the media account from `mediaInfo.UserID` before the download.

This is deliberately request-scoped. A global `SetProxy()` call per tenant is prohibited because it can cross-contaminate concurrent downloads.

## Failure semantics

When dynamic resolution is configured:

- no account/login identity -> error;
- no internal bearer token -> error;
- resolver transport error -> error;
- non-2xx response -> error;
- empty or malformed `proxy_url` -> error;
- E2EE/media proxy resolution failure -> operation fails;
- no code path may convert these failures into direct host egress.

Static proxy behavior remains available when `get_proxy_from` is empty; this preserves upstream single-proxy deployments while making the control-plane deployment fail closed.

## CI gate

Phase 3 cannot merge to `dev` until CI proves at minimum:

1. the deterministic patch applies to the pinned upstream SHA;
2. upstream Go tests pass after the patch;
3. the patched mautrix image builds;
4. resolver unit tests prove query context and bearer auth without logging the token;
5. first-login context is available before the first Meta validation request;
6. established login context contains both account and login identity;
7. E2EE requests use traffic class `e2ee`;
8. media requests use a request-scoped proxy rather than mutating the global transport;
9. the real Compose lane uses the patched image and the control-plane resolver before Phase 3 is called complete.

## Explicit incompleteness rule

A green compile-only patch is not Phase 3 complete. Until the real Compose lane proves the patched bridge talks to the control plane and no relevant path can fall back to direct host egress, the branch remains implementation-in-progress.