# mautrix-meta Fork Delta Contract

Status: normative for `dev` and the pinned fork delta
Pinned upstream baseline: `mautrix/meta v0.2607.0` (`ed37c9e6ce47e83dc75b9abea7b636302715b9bc`)
Integration branch: `dev`

## Purpose

The fork exists only to make Meta egress resolution account-aware, propagate one tenant-specific egress assignment across relevant Meta traffic classes, and bind first-login credentials to one pre-created control-plane connection before provider traffic. It must not become a general fork of mautrix-meta.

## Upstream limitations being corrected

Upstream `MetaConnector.getProxy(reason)` is connector-global and only sends `reason` to `get_proxy_from`. It does not identify the `UserLogin`, Meta account or Matrix owner. Media and E2EE paths also contain static/global proxy handling that cannot prove tenant-specific isolation.

Upstream cookie login also has no concept of the control plane's pre-created `meta_connection`: it can receive cookies and contact Meta without first proving which tenant-scoped connection/egress assignment owns those credentials.

## Deterministic source reconstruction

The full mautrix source remains outside this stack repository. Ordered applicators under `mautrix-meta-fork/` transform only the exact pinned upstream tree. Replacements assert expected source shape so upstream drift fails visibly.

Current ordered applicators for the deployable candidate are:

1. `apply_patch.py` — core account-aware resolver/media delta;
2. `apply_phase3_fixups.py` — fail-closed configuration, reconnect, E2EE and media hardening;
3. `apply_phase3_transport_tests.py` — observable login/messaging transport tests;
4. `apply_phase3_resolver_hardening.py` — bounded private resolver transport and strict validation;
5. `apply_provisioning_bootstrap.py` — BridgeV2 claim step, control-plane consume/bind calls and bootstrap-proxy installation;
6. `apply_provisioning_bootstrap_fixups.py` — exact pinned-source compile fixups for generated cookie regex literals and the static Messenger Lite caller.

The final fixup script is maintenance debt, not a separate runtime feature. Once behavior is stable, these provisioning transformations SHOULD be consolidated into a cleaner single applicator without changing the tested delta.

`Validate`, the dedicated provisioning acceptance workflow, Phase 6's fork lane, and `mautrix-meta-fork/Dockerfile` must apply the same ordered runtime delta before their relevant tests/builds. A workflow that omits the provisioning applicators is not evidence for the provisioning-enabled candidate.

## Account-aware egress context

The patched source carries, when available:

- `meta_account_id`
- `login_id`
- `reason`
- `traffic_class`

Traffic classes are `login`, `messaging`, `media`, and `e2ee`. Resolver requests use `network.get_proxy_from` and authenticate with `MAUTRIX_META_EGRESS_TOKEN` in `Authorization`, never the query string.

### Supported identity modes

Dynamic account-aware egress remains intentionally restricted to Facebook/Messenger on the pinned baseline.

Instagram is not claimed because its initial `ds_user_id` and later FBID-derived BridgeV2 login identity are not yet represented as an explicit identity transition. Messenger Lite remains rejected in dynamic mode because native login performs provider I/O before a stable account identity is available. Static upstream modes still have to compile and remain upstream-compatible.

Unsupported dynamic modes fail during config validation rather than borrowing connector-global identity or host egress.

## Provisioned first cookie login

For dynamic Facebook/Messenger cookie login, the first BridgeV2 step is now:

```text
fi.mau.meta.provisioning_claim
```

with `LoginStepTypeUserInput` and a token field. Cookies are not requested until a syntactically valid one-time claim has been supplied to the same `LoginProcess`.

After cookies are submitted:

1. the fork validates required cookies locally;
2. extracts Facebook `c_user` locally;
3. POSTs only `claim + meta_account_id + matrix_owner_mxid` to `/internal/v1/provisioning/consume` using the same dedicated private control-plane HTTP client as resolver traffic;
4. does not send Meta cookies to the control plane;
5. requires a successful confidential consume response containing the exact pre-created connection and its proxy assignment;
6. validates the returned proxy scheme/authority;
7. installs that proxy on the actual Messagix provider HTTP client;
8. only then permits `LoadMessagesPage` or other Meta-bound validation traffic.

If claim consumption fails, provider transport does not begin. There is no direct-host fallback.

The consume transaction spends the claim and binds `c_user` before the provider request. If Meta later rejects the cookies, that claim is intentionally not reusable; a new claim is issued for the same connection. A different account identity remains blocked by the control plane.

### Why bootstrap proxy is separate from normal resolution

The normal control-plane resolver remains active-connection-only. Weakening it to accept arbitrary draft connections would broaden the steady-state trust boundary.

Provisioning therefore returns the already-validated assigned proxy as part of the one-time consume response. This is the only bridge across the pre-active bootstrap window. Successful consumption moves a draft connection to `ready`, not `active`.

### Binding the BridgeV2 login identity

After the provider response establishes the deterministic BridgeV2 login identity but before the new login is persisted, the fork POSTs:

```text
connection_id + meta_account_id + mautrix_login_id + matrix_owner_mxid
```

to `/internal/v1/provisioning/bind-login`.

The control plane verifies that all identities still refer to the same connection and that the login ID is not owned elsewhere. Failure aborts the login rather than silently reassigning identity.

After this bootstrap, the existing account/login-aware messaging resolver transition occurs before `NewLogin` persistence and before the first MQTT connection.

## Established messaging connections

`MetaClient.proxyContext()` derives provider account identity from persisted cookies and includes `UserLogin.ID`. Normal connect and cached reconnect use the same account-aware proxy setup helper. Re-resolution changes the reason, not the sticky assignment.

## E2EE

Dynamic E2EE resolves the account-specific proxy immediately before the Whatsmeow connection. Resolution failure, an empty proxy, or `SetProxyAddress` failure prevents socket connection rather than falling back to host egress.

New E2EE device registration on the pinned upstream occurs earlier through the already account-proxied Messagix transport. This preserves egress isolation, although that registration is classified through messaging rather than a separate `e2ee` resolver call.

## Media and avatars

Upstream `mediadl` has a process-global `http.Client`; mutating it per tenant would create a cross-tenant race. The fork uses request-scoped proxy context and a cloned baseline transport instead.

Context is injected at identified pinned-baseline entry points including direct media, Meta-to-Matrix attachment conversion, account-bound avatars and chunked media requests. When dynamic media egress is enabled, missing proxy context fails closed.

## Internal control-plane transport hardening

`get_proxy_from` must be `http` or `https` with a host and no embedded userinfo, pre-existing query or fragment.

Resolver and provisioning internal requests use the dedicated `proxyResolverHTTPClient`, which:

- ignores ambient `HTTP_PROXY`/`HTTPS_PROXY` (`Proxy=nil`);
- has a five-second timeout;
- refuses redirects;
- keeps bearer credentials off query strings;
- limits response bodies to 64 KiB;
- validates returned proxy URLs as `http`, `https`, or `socks5` with explicit host/port and no path/query/fragment.

Provisioning internal endpoints are derived from the same configured control-plane origin rather than accepting a second arbitrary service destination.

## Failure semantics

For the dynamic proxy-required path, all of the following fail closed:

- missing/invalid/expired/used/revoked provisioning claim;
- Matrix-owner, tenant, Meta-account or login-ID conflict;
- missing stable account identity;
- missing internal bearer token;
- invalid control-plane endpoint;
- resolver/provisioning timeout or unavailability;
- redirect or non-2xx internal response;
- oversized/malformed internal response;
- missing/unhealthy assignment or missing secret;
- malformed/unsupported proxy URL;
- provider proxy installation failure;
- missing media proxy context;
- E2EE proxy resolution/setup failure.

No successful dynamic error path is allowed to degrade to host/Contabo direct Meta traffic.

Static upstream proxy behavior remains supported when `get_proxy_from` is empty so the fork does not unnecessarily break single-proxy deployments.

## Packaging

`mautrix-meta-fork/Dockerfile` performs the exact pinned checkout and complete ordered runtime patch application during image construction. The source delta is reconstructable from the pinned SHA and repository scripts.

This is deterministic source reconstruction, not bit-for-bit hermetic image reproduction: base image tags, OS repositories and some CI actions are not yet digest/SHA pinned.

## CI proofs

The combined fork gates are intended to prove:

- exact application to pinned upstream;
- `gofmt` and `git diff --check` on generated/touched source;
- full upstream/fork Go regression tests;
- strict resolver context/auth/timeout/redirect/response validation;
- unsupported dynamic mode rejection;
- provisioning claim step precedes cookies in dynamic login;
- failed claim consumption produces zero provider direct-sentinel hits;
- successful consumption installs the returned bootstrap proxy on the actual Messagix provider HTTP client, whose direct sentinel remains at zero;
- exact connection/account/login/Matrix-owner binding request after provider identity establishment;
- normal login→messaging transition before login persistence;
- normal connect and cached reconnect use account/login context;
- login/messaging/media controlled transports reach proxy fixtures rather than direct sentinels;
- media missing-context and E2EE proxy failures fail closed;
- two account contexts use distinct actual proxy transports in the Phase 6 A/B proof;
- patched image builds and topology startup with the same provisioning-enabled fork;
- control-plane secret/canary material is absent from service logs.

A provisioning capability is not complete until these proofs are green on one exact candidate SHA and again on the resulting `dev` merge SHA.

## Evidence boundary

CI does not use live Meta credentials plus externally observable production-like residential exits. It therefore cannot truthfully prove that a live provider request reached Meta from a particular public IP, nor can it fully observe a real Whatsmeow E2EE socket through that exit.

Controlled staging remains mandatory: use disposable supported Facebook/Messenger accounts, two observable account-specific egress endpoints, exercise initial login, reconnect, media and E2EE, verify exit assignment, and inject resolver/proxy failure while confirming absence of host direct fallback.

## Known improvement areas

- Request-scoped media transport cloning has a performance/resource cost under heavy chunked-media load.
- The provisioning patch currently needs a small exact-source fixup script after generation; consolidate after correctness is stable.
- CI has strong E2EE resolver/setup failure evidence but not a live external Whatsmeow exit-IP proof.
- Base image/action/native package inputs are not fully immutable.

## Scope constraints

The delta is restricted to account-aware egress context, first-login provisioning/binding, the minimum login/connect/E2EE call sites, and media context propagation needed to preserve tenant isolation. Chatwoot integration, tenant business logic, admin UI, Meta cookie storage outside mautrix, CRM behavior, generic proxy-pool rotation, Instagram dynamic identity mapping and Messenger Lite pre-identity routing are non-goals.

If a future upstream change requires a broad protocol rewrite, implementation stops for architecture reassessment rather than expanding this fork casually.

## Upgrade discipline

Every upstream upgrade must:

1. identify the new upstream tag/SHA and prior patched tag/SHA;
2. make deterministic patch application fail visibly against changed source;
3. inspect and deliberately update the smallest affected replacements;
4. run upstream tests plus provisioning/resolver/media/transport tests;
5. run control-plane contract tests;
6. rebuild/start the patched topology;
7. run controlled external egress acceptance before any human-authorized production promotion.

## Required release proofs

Before a human could consider promoting an exact `dev` candidate to `main`, evidence must include:

- provisioning claim binds the correct supported account/connection before first Meta-bound request;
- two supported accounts can use distinct assigned egress;
- reconnect preserves assignment;
- message media, direct media, avatars and E2EE obey isolation policy;
- required resolver/proxy failure produces no direct-host Meta request;
- secrets and raw provisioning claims remain absent from logs/persistence;
- the fork remains reconstructable from the pinned upstream baseline;
- real staging confirms externally observable provider egress and round trips.

Passing these proofs still does not authorize `main`; that remains an explicit human decision for the exact SHA.