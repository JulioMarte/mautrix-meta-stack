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

## Required bootstrap mechanism

The control plane MUST create a `meta_connection` before Meta authentication is attempted. A connection in `draft` state contains at minimum:

```text
id
tenant_id
matrix_owner_mxid
egress_profile_id
egress_policy
status=draft
```

The connection then receives a short-lived, single-purpose `provisioning_claim` or equivalent opaque bootstrap identifier. The claim binds:

```text
claim_id
meta_connection_id
tenant_id
matrix_owner_mxid
expires_at
used_at
```

The raw claim secret MUST NOT be persisted in plaintext; a one-way digest is preferred.

The exact UI transport may evolve, but the login path MUST carry enough context to associate the submitted Meta credentials with exactly one pre-created `meta_connection` before any Meta-bound request.

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

Normative sequence:

```text
operator creates tenant
-> operator creates meta_connection
-> egress assignment is attached
-> control plane issues provisioning claim
-> authorized Matrix user starts Meta login for that connection
-> mautrix receives submitted Facebook cookies
-> fork extracts c_user locally
-> fork resolves provisioning claim + meta_account_id against control plane
-> control plane atomically binds meta_account_id to meta_connection_id
-> egress is resolved for that exact connection
-> only then may first Meta-bound request occur
-> successful BridgeV2 login produces mautrix_login_id
-> control plane binds mautrix_login_id to same meta_connection
-> connection may transition toward ready/active
```

The control plane MUST NOT receive or store Meta cookies. The fork extracts only the required account identifier from the already-submitted cookie set.

## Atomic binding rules

Binding `meta_account_id` MUST be transactional and conflict-safe.

At minimum:

- one active Meta account identity cannot be bound to two active `meta_connection` records unless a future explicit shared-account model is introduced;
- the provisioning claim must belong to the target connection;
- the claim must be unused and unexpired;
- the Matrix principal performing the login must match the connection authorization policy;
- the tenant and connection must be enabled for provisioning;
- an existing conflicting account binding returns a terminal conflict rather than being overwritten;
- egress assignment must satisfy the connection policy before Meta traffic is allowed.

## Re-login and replacement credentials

A later re-login for an existing connection does not create a new connection automatically. It must resolve to the already-bound `meta_connection_id` and `meta_account_id`.

If submitted credentials identify a different `meta_account_id`, the operation MUST stop and require an explicit operator decision. Silent account replacement is forbidden because it could route a different person's messages into an existing tenant/Chatwoot binding.

## Connection lifecycle

Suggested lifecycle:

```text
draft
-> provisioning
-> authenticated
-> ready
-> active
```

Operational side states may include `degraded`, `blocked`, and `disabled`.

`active` requires at minimum a valid tenant, bound Meta account, valid mautrix login identity, required egress assignment, and required Chatwoot binding for production routing.

## Revocation

Operators need an explicit operation to revoke a provisioning claim and to disconnect/disable a Meta connection without deleting routing history.

Revoking or disabling a connection MUST prevent future routing and new egress resolution for protected traffic as quickly as practical.

## Required CI proofs

CI MUST prove:

- a draft connection can receive a provisioning claim;
- expired/used/forged claims are rejected;
- one Matrix user can provision two distinct connections without ambiguity;
- two tenants with similar identities cannot cross-bind;
- first-login `c_user` is bound to the intended connection before any Meta-bound request;
- conflicting `meta_account_id` binding fails rather than overwrites;
- re-login with the same account preserves connection identity and egress;
- re-login with a different account is blocked;
- no Meta cookies enter control-plane persistence or logs.