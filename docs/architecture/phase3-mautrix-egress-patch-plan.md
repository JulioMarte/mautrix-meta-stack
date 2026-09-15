# Phase 3 — mautrix-meta account-aware egress patch

Status: implementation contract
Base stack branch: `dev@f8ab8448ce11a6d86c73a3cbbf52de7e84a714ae`
Upstream mautrix baseline: `mautrix/meta@ed37c9e6ce47e83dc75b9abea7b636302715b9bc` (`v0.2607.0`)
Integration target: `dev`

## Goal

Make one mautrix-meta process resolve egress using stable account/login identity, without broad protocol refactoring and without allowing a `proxy_required` connection to fall back to host egress.

## Patch shape

The stack repository does not vendor the whole mautrix source tree. Phase 3 stores ordered deterministic patch applicators under `mautrix-meta-fork/`. CI clones the exact upstream commit, applies replacements that assert their expected source text, runs the upstream Go test suite, and builds the patched image.

If an upstream source fragment no longer matches, patch application fails. That is intentional: an upstream upgrade must be reviewed rather than silently receiving a fuzzy patch.

## Supported scope

Dynamic account-aware egress on this pinned baseline supports Facebook/Messenger cookie flows only.

Instagram is explicitly blocked for dynamic egress until the control-plane identity model represents the transition between the first cookie identity (`ds_user_id`) and the later FBID-derived BridgeV2 login identity. Messenger Lite is also blocked because its native login performs network I/O before a stable account identity is available.

Unsupported modes fail during config validation; they must not fall back to connector-global or host egress.

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

The resolver endpoint itself must be an `http`/`https` URL with a host and without embedded credentials, pre-existing query parameters or a fragment. Resolver I/O uses a dedicated non-environment-proxied client, a bounded timeout, no redirects, and a bounded response body.

## Four traffic paths

### 1. First login

Facebook/Messenger cookie login has a stable account ID through `Cookies.GetUserID()` (`c_user`). The patch binds a `login` resolver callback before `UpdateProxy("login")` and before `LoadMessagesPage`.

A proxy-enabled flow that does not yet expose a supported stable account ID must fail closed rather than borrow a connector-global identity.

### 2. Messaging/connect/reconnect

An established `MetaClient` has `UserLogin.ID` plus persisted cookies. Its resolver callback includes both identifiers and uses traffic class `messaging`. Normal connect and cached reconnect share the same account-aware proxy setup helper so the cache path cannot bypass resolver installation.

The control plane may initially know only `meta_account_id`. When mautrix later supplies both account and login IDs, lookup accepts the additional ID if the stored login ID is still `NULL`, provided the known identifier uniquely selects one active connection. Conflicting or already-bound contradictory identities fail closed.

### 3. E2EE

The upstream E2EE path uses a connector-global static proxy. The patch resolves the same account-specific egress immediately before configuring the Whatsmeow client and refuses to connect when resolution or `SetProxyAddress` fails.

Initial E2EE device registration happens through the existing Messagix client before the Whatsmeow socket is prepared. On the pinned upstream that Messagix client is already account-proxied, so isolation is preserved even though that registration request is classified through messaging rather than a separate E2EE resolver call.

### 4. Media

The upstream media downloader owns a process-global `http.Client`; mutating its transport proxy per account would race under concurrent tenants. Phase 3 therefore adds a request-context proxy override: a download clones the baseline transport and applies the account-specific proxy only to that request chain.

Media context is injected for normal attachment conversion, direct media and avatars. Chunked HEAD/GET operations inherit the same proxy context. When dynamic media egress is enabled, a download with no proxy context fails closed.

## Failure semantics

When dynamic resolution is configured:

- no stable account/login identity -> error;
- conflicting identities -> error;
- no internal bearer token -> error;
- unsafe resolver URL -> config validation error;
- resolver timeout/transport error -> error;
- redirect response -> error without following it;
- non-2xx response -> error;
- oversized or malformed resolver body -> error;
- empty, malformed or unsupported `proxy_url` -> error;
- E2EE/media proxy resolution or setup failure -> operation fails;
- no code path may convert these failures into direct host egress.

Static proxy behavior remains available when `get_proxy_from` is empty; this preserves upstream single-proxy deployments while making the control-plane deployment fail closed.

## CI gate

Phase 3 cannot merge to `dev` until the exact candidate SHA proves at minimum:

1. all ordered patch applicators apply to the pinned upstream SHA;
2. upstream Go tests pass after the patch;
3. the patched mautrix image builds through both the fork job and stack Docker recipe;
4. resolver tests prove identity/query context, bearer auth, timeout, redirect rejection, response bounds and strict proxy URL validation;
5. first-login resolver setup occurs before provider HTTP traffic;
6. established connect and cached reconnect use account-aware context;
7. observable login and messaging transports hit a proxy fixture while a direct sentinel receives no request;
8. media requests use request-scoped proxy transport and missing scoped context fails closed;
9. E2EE resolution/setup failures prevent connection;
10. the control plane accepts progressive identity only when non-conflicting;
11. the real Compose lane uses the patched image, authenticates to the resolver and verifies secret non-leakage;
12. Phase 3 topology CI runs for Phase 3 feature/fix changes and `dev`.

## Evidence boundary

CI cannot prove the public exit IP of a live Meta request without disposable provider credentials and externally observable proxies. Before production promotion, staging must exercise supported Facebook/Messenger login, reconnect, media and E2EE while observing per-account exits and proving resolver/proxy failure never falls back to the host IP.

Instagram enters this matrix only after its identity transition is modeled and implemented.

## Explicit incompleteness rule

A green compile-only patch is not Phase 3 complete. A green deterministic unit suite is also insufficient by itself. The candidate must pass the real patched Compose topology, and production promotion still requires the external egress acceptance proof described above.
