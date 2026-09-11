# Meta Control Plane Onboarding and Identity Binding Contract

Status: normative for `feature/meta-control-plane`

## Purpose

This document closes the bootstrap problem between tenant identity, Matrix operator identity, Meta account identity, and the first account-specific egress assignment.

A `meta_connection` may exist before the Facebook numeric account identifier is known. The system MUST still be able to select the correct tenant-scoped egress before the first Meta-bound request without guessing from a Matrix user or silently creating an unowned connection.

## Identity concepts

The following identities are distinct:

- `tenant_id`: customer security boundary.
- `meta_connection_id`: internal opaque identity for one configured Meta connection.
- `matrix_owner_mxid`: Matrix principal allowed to initiate/manage the connection.
- `meta_account_id`: Facebook numeric account identity derived from `c_user` during login.
- `mautrix_login_id`: BridgeV2 `UserLogin.ID` after the login is established.

No identity may be substituted for another merely because values happen to correlate.

## Implemented bootstrap mechanism

The control plane creates a `meta_connection` before Meta authentication is attempted. A connection in `draft` state contains at minimum:

```text
id
tenant_id
matrix_owner_mxid
egress_profile_id
egress_policy=proxy_required
status=draft
```

Dynamic provisioning currently requires `proxy_required`. `direct_allowed` and `proxy_preferred` are not accepted for this bootstrap because the first provider-bound request must have a fail-closed tenant-scoped proxy assignment.

The control plane issues a short-lived, single-purpose provisioning claim through:

```text
POST /api/v1/meta-connections/:id/provisioning-claims
```

The claim record binds:

```text
claim_id
secret_digest
meta_connection_id
tenant_id
matrix_owner_mxid
expires_at
used_at
revoked_at
created_at
```

The raw claim secret is returned only when issued. Only its SHA-256 digest is persisted. Issuing a new pending claim for a connection revokes older unused claims for that connection.

BridgeV2 dynamic cookie login starts with a `user_input` token step named `fi.mau.meta.provisioning_claim`. The cookie step is not returned until the caller supplies a syntactically valid claim. The claim stays in the login-process memory and is not mixed into Meta cookies.

## Why Matrix identity alone is insufficient

One Matrix user may legitimately own multiple Meta logins. Therefore this is forbidden:

```text
matrix_owner_mxid -> choose first matching connection
```

and this is also forbidden:

```text
unknown c_user -> create connection under whichever tenant owns the Matrix user
```

Both are ambiguous and create cross-tenant or wrong-account risk.

## First-login sequence

Implemented sequence for supported dynamic Facebook/Messenger cookie login:

```text
operator creates tenant
-> operator creates draft meta_connection
-> operator assigns healthy proxy_required egress
-> control plane issues one-time provisioning claim
-> authorized Matrix user starts Meta login
-> fork requests provisioning claim as first BridgeV2 user_input step
-> after claim input, fork accepts submitted Facebook cookies
-> fork extracts c_user locally
-> fork POSTs claim + c_user + matrix_owner_mxid to /internal/v1/provisioning/consume
-> control plane atomically validates/consumes claim and binds c_user to exactly one meta_connection
-> draft connection transitions to ready
-> consume response returns that connection's confidential bootstrap proxy assignment
-> fork installs that proxy on the actual Messagix provider HTTP client
-> only then may the first Meta-bound request occur
-> provider authentication establishes the BridgeV2 login identity
-> fork POSTs connection + c_user + mautrix_login_id + matrix_owner_mxid to /internal/v1/provisioning/bind-login
-> control plane binds mautrix_login_id to the same meta_connection
-> production routing still requires explicit connection activation
```

The normal egress resolver continues to require an active connection. Bootstrap does not weaken that rule: the one-time consume response carries the already-validated assigned proxy solely to bridge the pre-active first-login window.

The control plane MUST NOT receive or store Meta cookies. The fork sends only the raw provisioning claim, extracted account identifier, Matrix owner identity, and later the BridgeV2 login identifier.

## Atomic binding rules

Binding `meta_account_id` is transactional and conflict-safe:

- one Meta account identity cannot be bound to two different `meta_connection` records;
- the claim resolves to exactly one pre-created connection;
- the claim must be unused, unrevoked and unexpired;
- the submitted Matrix owner must equal the claim and connection owner;
- claim tenant and connection tenant must agree;
- the tenant and connection must remain provisionable;
- existing conflicting account binding returns a terminal conflict rather than being overwritten;
- dynamic provisioning requires assigned healthy `proxy_required` egress and resolvable proxy credentials;
- the claim is marked used in the same database transaction that binds `meta_account_id`.

If the provider later rejects the cookies, the consumed claim is not reusable. An operator issues a new claim for the same connection. The existing same-account binding is preserved; a different `meta_account_id` remains blocked.

## Re-login and replacement credentials

A later re-login for an existing connection does not create a new connection automatically. A new one-time claim may target the existing connection, but submitted cookies must resolve to the already-bound `meta_account_id`.

If submitted credentials identify a different `meta_account_id`, the operation stops with `META_ACCOUNT_CONFLICT`. Silent account replacement is forbidden because it could route another account's messages into an existing tenant/Chatwoot binding.

Binding `mautrix_login_id` is also conflict-safe: the account, connection and Matrix owner must still match, and a login ID already belonging to a different connection cannot be reassigned silently.

## Connection lifecycle

The currently implemented bootstrap lifecycle is:

```text
draft
-> ready          # provisioning claim consumed and meta_account_id bound
-> active         # explicit operator activation after required production dependencies are valid
```

Operational side states include `degraded`, `blocked`, and `disabled`.

`ready` is intentionally not sufficient for Matrix/Chatwoot production routing or the normal active-only egress resolver. `active` remains the production-routing state.

## Revocation

An unused claim can be revoked through:

```text
POST /api/v1/meta-connections/:id/provisioning-claims/:claimId/revoke
```

Used claims cannot be revoked because their one-time authority has already been consumed. Disabling/blocking a connection prevents new provisioning consumption and normal active routing.

## Required CI proofs

Repository acceptance must prove:

- a draft connection with valid fail-closed egress can receive a provisioning claim;
- only a digest, never the raw claim, is persisted;
- expired/used/revoked/forged claims are rejected;
- `direct_allowed` cannot be provisioned through the dynamic bootstrap;
- one Matrix user can provision two distinct connections without ambiguity;
- two tenants with similar identities cannot cross-bind;
- first-login `c_user` is bound to the intended connection before any provider-bound request;
- the actual Messagix provider HTTP client uses the bootstrap proxy and a direct sentinel receives zero requests;
- failed claim consumption prevents provider transport;
- conflicting `meta_account_id` binding fails rather than overwrites;
- re-login with the same account preserves connection identity and egress;
- re-login with a different account is blocked;
- `mautrix_login_id` is bound only to the already-provisioned connection;
- no Meta cookies, raw claim secrets or proxy credentials enter control-plane persistence or audit logs.

These deterministic proofs do not replace final staging with real Meta accounts and real egress endpoints.