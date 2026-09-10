# mautrix-meta Fork Delta Contract

Status: normative for `dev` and Phase 3
Pinned upstream baseline: `mautrix/meta v0.2607.0` (`ed37c9e6ce47e83dc75b9abea7b636302715b9bc`)
Integration branch: `dev`

## Purpose

The fork exists only to make Meta egress resolution account-aware and to propagate one tenant-specific egress assignment across every relevant Meta traffic class. It must not become a general fork of mautrix-meta.

## Upstream limitation being corrected

Upstream `MetaConnector.getProxy(reason)` is connector-global and only sends `reason` to `get_proxy_from`. It does not identify the `UserLogin`, Meta account or Matrix owner. Media and E2EE paths also contain static/global proxy handling that cannot prove tenant-specific isolation.

## Implemented delta

Phase 3 keeps the full mautrix source out of this stack repository. `mautrix-meta-fork/apply_patch.py` and `apply_phase3_fixups.py` deterministically transform the exact pinned upstream source. Each textual replacement asserts the expected source shape; upstream drift therefore causes patch application to fail instead of silently fuzzing the delta onto changed code.

The patched source introduces an account-aware proxy context containing, when available:

- `meta_account_id`
- `login_id`
- `reason`
- `traffic_class`

Traffic classes are `login`, `messaging`, `media`, and `e2ee`. Resolver requests use the existing `network.get_proxy_from` configuration and authenticate with `MAUTRIX_META_EGRESS_TOKEN` in the HTTP `Authorization` header. The token is never placed in the query string.

### First cookie login

`getMessagixClient` derives the stable provider identity from `Cookies.GetUserID()` before `UpdateProxy("login")` and before `LoadMessagesPage`. On the pinned upstream this maps to the provider cookie identity (`c_user` for Facebook/Messenger-family cookie flows and `ds_user_id` for Instagram).

If dynamic egress is enabled but no stable account identity is present, the proxy-enabled login fails before the Meta validation request. Native login flows that cannot yet supply stable identity are therefore not allowed to borrow connector-global identity.

### Established messaging connections

`MetaClient.proxyContext()` derives provider account identity from persisted cookies and includes `UserLogin.ID`. Connect/reconnect installs an account-bound resolver callback while preserving the runtime `reason` argument used by messagix when it refreshes a proxy.

### E2EE

The upstream static `SetProxyAddress(m.Config.Proxy)` path is replaced for dynamic egress by an account-specific resolver call immediately before the E2EE connection. Resolution failure or an empty proxy prevents the E2EE connection rather than falling back to host egress.

### Media and avatars

Upstream `mediadl` owns a process-global `http.Client`. Mutating that global transport per tenant would be a cross-tenant race. The fork therefore adds a request-context proxy override that clones the baseline `http.Transport` and applies the account-specific proxy only to that request chain.

Account-aware media context is injected at all identified network entry points on the pinned baseline:

- direct media downloads using `mediaInfo.UserID`;
- normal Meta-to-Matrix message conversion before attachment reupload;
- account-bound avatar downloads;
- chunked media HEAD/GET requests through the same request-scoped client selection.

Other `wrapAvatar` call sites on `MetaClient` are explicitly receiver-bound so they cannot accidentally fall back to an unscoped global helper.

## Failure semantics

When dynamic resolution is configured:

- missing stable identity -> fail;
- missing internal bearer token -> fail;
- resolver unavailable -> fail;
- non-2xx resolver response -> fail;
- missing assignment or unhealthy required egress -> fail at the control plane;
- empty or malformed `proxy_url` -> fail;
- proxy setup failure -> fail;
- no direct-host fallback is permitted for the patched proxy-required path.

Static upstream proxy behavior remains supported when `get_proxy_from` is empty so the fork does not unnecessarily break single-proxy deployments.

## Reproducible packaging

`mautrix-meta-fork/Dockerfile` performs the same pinned checkout and deterministic patch during image construction. `compose.phase3.yaml` selects that image for the mautrix runtime and injects `MAUTRIX_META_EGRESS_TOKEN` from the control-plane internal token.

This means the deployable artifact is reconstructed from:

1. the exact upstream SHA;
2. the two audited patch applicators;
3. the stack Docker recipe.

No mutable local mautrix checkout is part of the release input.

## CI proofs

The current Phase 3 gates prove:

- deterministic application to `ed37c9e6ce47e83dc75b9abea7b636302715b9bc`;
- `gofmt`/`git diff --check` on touched source;
- full upstream `go test ./...` with native libolm dependency installed;
- fork resolver tests for identity/query context, bearer auth and fail-closed missing identity/token;
- media test proving request-scoped transport selection does not mutate the process-global transport;
- patched image build using upstream's native Docker contract;
- independent stack Dockerfile build from the pinned upstream;
- Compose startup of the patched mautrix runtime alongside Synapse and the control plane;
- runtime configuration of `get_proxy_from` and proxy traffic flags;
- explicit injection of the internal resolver credential into the mautrix container;
- authenticated resolver reachability from inside that patched runtime container;
- rejection of proxy/internal-token leakage in service logs.

## Evidence boundary

CI does **not** contain real disposable Facebook/Instagram credentials plus an externally observable egress proxy. Therefore it cannot truthfully prove that a live provider request reached Meta from a particular public exit IP.

That final external property must be verified in a controlled staging environment before production promotion: use disposable provider accounts, observable per-account proxy exits, exercise first login, reconnect, media and E2EE, and verify the egress IP/assignment while also testing resolver/proxy failure for absence of direct fallback.

This limitation is not a reason to weaken CI; it defines the remaining staging acceptance test.

## Scope constraints

The delta is limited to resolver context, the minimum login/connect/E2EE call sites, and media context propagation needed to eliminate process-global per-account routing. Unrelated protocol behavior, formatting, persistence and BridgeV2 semantics remain upstream-equivalent.

If a future upstream change requires a broad protocol rewrite, implementation stops for architecture reassessment rather than expanding this fork casually.

## Upgrade discipline

Every upstream upgrade must:

1. identify the new upstream tag/SHA and prior patched tag/SHA;
2. make the deterministic patch fail visibly against changed source;
3. inspect and deliberately update the smallest affected replacements;
4. run upstream tests plus fork-specific resolver/media tests;
5. run control-plane contract tests;
6. rebuild and start the patched Compose topology;
7. run the controlled external egress acceptance test before production promotion.

## Required release proofs

Before promoting `dev` to `main`, the release evidence must include both CI and staging:

- login context identifies the correct Meta account before first Meta-bound request;
- two accounts resolve distinct assigned egress when configured that way;
- reconnect preserves the same account assignment;
- message media, direct media, avatars and E2EE obey the account isolation policy;
- a failed required resolver/proxy produces no direct-host Meta request;
- secrets remain absent from logs;
- the patch remains reproducible from the pinned upstream baseline.

## Non-goals

The fork does not implement Chatwoot integration, tenant business logic, admin UI, Meta cookie storage outside mautrix, CRM behavior, or generic proxy-pool rotation.
