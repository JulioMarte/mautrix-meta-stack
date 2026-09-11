# mautrix-meta Fork Delta Contract

Status: normative for `dev` and Phase 3
Pinned upstream baseline: `mautrix/meta v0.2607.0` (`ed37c9e6ce47e83dc75b9abea7b636302715b9bc`)
Integration branch: `dev`

## Purpose

The fork exists only to make Meta egress resolution account-aware and to propagate one tenant-specific egress assignment across every relevant Meta traffic class. It must not become a general fork of mautrix-meta.

## Upstream limitation being corrected

Upstream `MetaConnector.getProxy(reason)` is connector-global and only sends `reason` to `get_proxy_from`. It does not identify the `UserLogin`, Meta account or Matrix owner. Media and E2EE paths also contain static/global proxy handling that cannot prove tenant-specific isolation.

## Implemented delta

Phase 3 keeps the full mautrix source out of this stack repository. Ordered deterministic patch applicators under `mautrix-meta-fork/` transform the exact pinned upstream source. Each textual replacement asserts the expected source shape; upstream drift therefore causes patch application to fail instead of silently fuzzing the delta onto changed code.

The current ordered applicators are:

1. `apply_patch.py` — core account-aware resolver/media delta;
2. `apply_phase3_fixups.py` — fail-closed configuration, reconnect, E2EE and media hardening plus regression tests;
3. `apply_phase3_transport_tests.py` — observable login/messaging transport tests;
4. `apply_phase3_resolver_hardening.py` — bounded resolver transport, redirect rejection, strict endpoint/response validation and associated tests.

The patched source introduces an account-aware proxy context containing, when available:

- `meta_account_id`
- `login_id`
- `reason`
- `traffic_class`

Traffic classes are `login`, `messaging`, `media`, and `e2ee`. Resolver requests use the existing `network.get_proxy_from` configuration and authenticate with `MAUTRIX_META_EGRESS_TOKEN` in the HTTP `Authorization` header. The token is never placed in the query string.

### Supported identity modes

Dynamic account-aware egress is intentionally restricted to Facebook/Messenger on the pinned baseline. Instagram is not claimed as supported because the first cookie identity (`ds_user_id`) and the later FBID-derived BridgeV2 `UserLogin.ID` are not yet modeled as an explicit identity transition in the control plane. Messenger Lite is also rejected because its native login performs network I/O before a stable account identity is available.

This restriction is fail-closed: an unsupported mode is rejected during config validation rather than allowed to borrow connector-global identity or host egress.

### First cookie login

For Facebook/Messenger cookie flows, `getMessagixClient` derives the stable provider identity from `Cookies.GetUserID()` (`c_user`) before `UpdateProxy("login")` and before `LoadMessagesPage`.

If dynamic egress is enabled but no stable account identity is present, the proxy-enabled login fails before the Meta validation request. Native login flows that cannot yet supply stable identity are therefore not allowed to borrow connector-global identity.

### Progressive identity

A control-plane connection may be pre-created with the provider account identity before BridgeV2 has persisted `UserLogin.ID`. Once mautrix has both identifiers, resolver lookup accepts the additional identifier when the corresponding stored column is still `NULL`, provided the other identifier uniquely selects one active connection. If supplied identifiers resolve to different active connections, or contradict a non-null persisted identifier, resolution fails with `IDENTITY_CONFLICT`.

This prevents the integration topology from depending on an artificial pre-populated `mautrix_login_id` while preserving fail-closed conflict handling.

### Established messaging connections

`MetaClient.proxyContext()` derives provider account identity from persisted cookies and includes `UserLogin.ID`. Normal connect and cached reconnect use the same account-aware proxy setup helper. Re-resolution changes the reason, not the account assignment.

### E2EE

The upstream static `SetProxyAddress(m.Config.Proxy)` path is replaced for dynamic egress by an account-specific resolver call immediately before the Whatsmeow connection. Resolution failure, an empty proxy, or `SetProxyAddress` failure prevents the E2EE socket connection rather than falling back to host egress.

New E2EE device registration occurs earlier through the already account-proxied Messagix client on the pinned upstream. This preserves egress isolation, although that registration traffic is classified through the messaging transport rather than as a separate `e2ee` resolver call.

### Media and avatars

Upstream `mediadl` owns a process-global `http.Client`. Mutating that global transport per tenant would be a cross-tenant race. The fork therefore adds a request-context proxy override that clones the baseline `http.Transport` and applies the account-specific proxy only to that request chain.

Account-aware media context is injected at all identified network entry points on the pinned baseline:

- direct media downloads using `mediaInfo.UserID`;
- normal Meta-to-Matrix message conversion before attachment reupload;
- account-bound avatar downloads;
- chunked media HEAD/GET requests through the same request-scoped client selection.

When dynamic media egress is enabled, `mediadl` refuses downloads with no scoped proxy context. This makes an uninstrumented future media call site fail closed instead of silently using the process-global transport.

## Resolver transport hardening

Dynamic resolver configuration is validated before runtime use. `get_proxy_from` must be an `http` or `https` URL with a host and without embedded userinfo, pre-existing query parameters or a fragment.

The internal resolver client:

- ignores ambient `HTTP_PROXY`/`HTTPS_PROXY` by using a dedicated transport with `Proxy=nil`;
- has a five-second timeout;
- does not follow redirects;
- limits resolver response bodies to 64 KiB;
- accepts returned proxy URLs only for `http`, `https`, or `socks5` with explicit host and port and no path/query/fragment.

The control plane independently validates supported proxy scheme, DNS/IP host, port, and username/secret-reference pairing before persistence and again at resolution time.

## Failure semantics

When dynamic resolution is configured:

- missing stable identity -> fail;
- conflicting identity -> fail;
- missing internal bearer token -> fail;
- invalid resolver endpoint configuration -> fail startup/config validation;
- resolver timeout/unavailability -> fail;
- resolver redirect -> fail without following it;
- non-2xx resolver response -> fail;
- oversized/malformed resolver response -> fail;
- missing assignment or unhealthy required egress -> fail at the control plane;
- empty, malformed, or unsupported `proxy_url` -> fail;
- proxy setup failure -> fail;
- no direct-host fallback is permitted for the patched proxy-required path.

Static upstream proxy behavior remains supported when `get_proxy_from` is empty so the fork does not unnecessarily break single-proxy deployments.

## Packaging

`mautrix-meta-fork/Dockerfile` performs the same pinned source checkout and ordered patch application during image construction. `compose.phase3.yaml` selects that image for the mautrix runtime and injects `MAUTRIX_META_EGRESS_TOKEN` from the control-plane internal token.

The source delta is deterministically reconstructed from the exact upstream SHA and audited patch applicators. The resulting container image is not claimed to be bit-for-bit hermetic because base image tags and distribution package repositories are not digest/version pinned.

## CI proofs

The current Phase 3 gates prove:

- deterministic application to `ed37c9e6ce47e83dc75b9abea7b636302715b9bc`;
- `gofmt`/`git diff --check` on touched source;
- full upstream `go test ./...` with native libolm dependency installed;
- resolver identity/query context, bearer authentication, timeout and missing-identity/token failure;
- redirect rejection, resolver response size bound and strict returned proxy URL validation;
- dynamic config rejection for unsafe resolver URLs, partial traffic coverage, static fallback and unsupported identity modes;
- first-login resolver invocation before provider transport use;
- normal connect and cached reconnect account/login context;
- observable HTTP tests in which login and messaging clients reach a proxy fixture while a direct sentinel receives zero requests;
- observable media test in which the request-scoped downloader reaches the proxy fixture and the direct sentinel receives zero requests;
- missing media proxy context fails closed when dynamic media egress is required;
- E2EE proxy resolution/setup failures prevent the Whatsmeow connection path;
- control-plane scheme/host/auth validation and progressive identity conflict behavior;
- patched image build and real Compose topology startup;
- runtime configuration of `get_proxy_from` and proxy traffic flags;
- authenticated resolver reachability from inside the patched runtime container;
- topology proof where only `meta_account_id` is prebound but a later request containing both account and login IDs resolves successfully;
- rejection of proxy/internal-token leakage in service logs.

The Phase 3 topology workflow runs on the Phase 3 feature branch, `fix/**` branches and `dev`, so security fixes cannot bypass the same deployable-artifact lane.

## Evidence boundary

CI does **not** contain disposable live Meta credentials plus externally observable production-like proxy exits. Therefore it cannot truthfully prove that a live provider request reached Meta from a particular public exit IP.

That final external property must be verified in a controlled staging environment before production promotion: use disposable supported Facebook/Messenger accounts, observable per-account proxy exits, exercise first login, reconnect, media and E2EE, and verify the egress IP/assignment while also testing resolver/proxy failure for absence of direct fallback.

Instagram requires a separate identity-model change before it can enter that acceptance matrix.

## Known improvement areas

The current request-scoped media implementation clones an `http.Transport` for each scoped request. This avoids cross-tenant proxy mutation, but under heavy chunked-media load it may create more transient transport/idle-connection state than desirable. A future optimization may reuse a scoped client for the lifetime of one media download without reintroducing shared mutable proxy state.

The current CI proves E2EE resolver and proxy-setup failure semantics but does not yet observe a real Whatsmeow socket through a local proxy sentinel. Staging remains required for the external E2EE exit-IP claim; a deterministic local socket-level fixture would strengthen CI further.

## Scope constraints

The delta is limited to resolver context, the minimum login/connect/E2EE call sites, and media context propagation needed to eliminate process-global per-account routing. Unrelated protocol behavior, formatting, persistence and BridgeV2 semantics remain upstream-equivalent.

If a future upstream change requires a broad protocol rewrite, implementation stops for architecture reassessment rather than expanding this fork casually.

## Upgrade discipline

Every upstream upgrade must:

1. identify the new upstream tag/SHA and prior patched tag/SHA;
2. make deterministic patch application fail visibly against changed source;
3. inspect and deliberately update the smallest affected replacements;
4. run upstream tests plus fork-specific resolver/media/transport tests;
5. run control-plane contract tests;
6. rebuild and start the patched Compose topology;
7. run the controlled external egress acceptance test before production promotion.

## Required release proofs

Before promoting `dev` to `main`, the release evidence must include both CI and staging:

- login context identifies the correct supported Meta account before first Meta-bound request;
- two supported accounts resolve distinct assigned egress when configured that way;
- reconnect preserves the same account assignment;
- message media, direct media, avatars and E2EE obey the account isolation policy;
- a failed required resolver/proxy produces no direct-host Meta request;
- secrets remain absent from logs;
- the patch remains reconstructable from the pinned upstream baseline.

## Non-goals

The fork does not implement Chatwoot integration, tenant business logic, admin UI, Meta cookie storage outside mautrix, CRM behavior, generic proxy-pool rotation, Instagram dynamic egress identity mapping, or Messenger Lite pre-identity egress routing.
