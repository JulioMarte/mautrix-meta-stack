# mautrix-meta Fork Delta Contract

Status: normative for `feature/meta-control-plane`
Pinned upstream baseline: `mautrix/meta v0.2607.0`

## Purpose

The fork exists only to make Meta egress resolution account-aware and to propagate one tenant-specific egress assignment across every relevant Meta traffic class. It must not become a general fork of mautrix-meta.

## Upstream limitation being corrected

Upstream `MetaConnector.getProxy(reason)` is connector-global and only sends `reason` to `get_proxy_from`. It does not identify the `UserLogin`, Meta account or Matrix owner. Media and E2EE paths also contain static-proxy handling that cannot prove tenant-specific isolation.

## Required behavior

The fork MUST expose an account-aware resolver context with, when available:

- `meta_account_id`
- `login_id`
- `reason`
- `traffic_class`

Traffic classes MUST distinguish at least `login`, `messaging`, `media`, and `e2ee`.

Before the first Facebook request during cookie login, `meta_account_id` MUST be derived from `c_user` and egress MUST be resolved first when policy is `proxy_required`.

For an established BridgeV2 `UserLogin`, connect and reconnect paths MUST resolve using that login/account identity.

For a given active connection, all traffic classes MUST use the same assigned egress profile unless an explicit future policy says otherwise.

## Failure semantics

For `proxy_required`:

- resolver unavailable -> fail connection/action;
- missing assignment -> fail;
- malformed proxy response -> fail;
- proxy setup failure -> fail;
- no direct-host fallback is permitted.

The fork MUST NOT silently reuse a connector-global proxy for a tenant-specific connection.

## Service authentication

Resolver calls MUST support a secret in an HTTP header such as `Authorization: Bearer <token>` or an equivalent dedicated header. Secrets MUST NOT be placed in query strings or logs.

## Scope constraints

Preferred changes are limited to the resolver abstraction and the smallest call sites necessary for login, connect/reconnect, media and E2EE. Unrelated protocol logic, formatting, event conversion, persistence and BridgeV2 behavior MUST remain upstream-equivalent.

If the delta becomes broad or requires invasive protocol rewrites, implementation MUST stop for architecture reassessment.

## Upgrade discipline

Every upstream upgrade MUST:

1. identify the new upstream tag and prior patched tag;
2. rebase/reapply only the documented fork delta;
3. run upstream tests plus fork-specific resolver tests;
4. run the control-plane contract tests;
5. run the real-container egress isolation lane before release.

## Required CI proofs

The fork is not acceptable unless CI proves:

- login context includes the correct Meta account before first Meta-bound request;
- two concurrent logins provide distinct resolver identities;
- reconnect preserves the same account assignment;
- media and E2EE follow the same tenant isolation policy;
- a failed required proxy never causes a direct-host request;
- secrets are absent from logs;
- the patch remains buildable against the pinned upstream baseline.

## Non-goals

The fork MUST NOT implement Chatwoot integration, tenant business logic, admin UI, Meta cookie storage outside mautrix, CRM behavior or generic proxy-pool rotation.