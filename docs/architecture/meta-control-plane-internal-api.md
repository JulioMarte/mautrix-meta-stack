# Meta Control Plane Internal API Contract

Status: normative for `feature/meta-control-plane`

## API surfaces

The service distinguishes operator management, internal service-to-service calls, Chatwoot webhooks and health endpoints:

```text
/admin/*
/api/v1/*
/internal/v1/*
/webhooks/chatwoot
/health/live
/health/ready
```

Internal endpoints MUST NOT be published as public Coolify domains.

## Authentication

`/internal/v1/*` MUST require service authentication using a header-carried secret. Query-string authentication is forbidden. Authentication failures return 401/403 without revealing whether a tenant, account, claim or assignment exists.

Management API authentication may initially be protected by Cloudflare Access plus application-side state-changing request protection. Production evolution SHOULD introduce explicit operator identity/RBAC rather than treating possession of network access as full authority.

## Provisioning bootstrap

Dynamic Facebook/Messenger cookie login requires an explicit one-time provisioning claim before the first provider-bound request. Matrix identity alone is not a selector for a Meta connection.

### POST `/api/v1/meta-connections/:id/provisioning-claims`

Operator-authenticated. Optional body:

```json
{ "ttlSeconds": 600 }
```

TTL must be between 60 and 1800 seconds. The target tenant/connection must be provisionable and must have healthy assigned `proxy_required` egress. `direct_allowed` and `proxy_preferred` do not satisfy this bootstrap.

Successful creation returns the raw claim only once together with claim metadata. Persistence stores only its digest. A newly issued pending claim revokes previous unused claims for the same connection.

### POST `/api/v1/meta-connections/:id/provisioning-claims/:claimId/revoke`

Operator-authenticated. Revokes an unused claim. A consumed claim cannot be made reusable by revocation.

### POST `/internal/v1/provisioning/consume`

Service-authenticated request:

```json
{
  "claim": "pc_<opaque>",
  "metaAccountId": "123456789",
  "matrixOwnerMxid": "@owner:example.com"
}
```

The fork derives `metaAccountId` locally from the submitted Facebook `c_user` cookie. Meta cookies themselves MUST NOT be sent to this endpoint.

The control plane atomically verifies that the claim is valid, unused, unrevoked and unexpired; tenant and Matrix owner agree with the pre-created connection; the submitted Meta account is not conflicting; and the assigned proxy remains eligible. It then consumes the claim and binds `meta_account_id` to that connection. A draft connection becomes `ready`.

Successful confidential response:

```json
{
  "connection_id": "opaque-connection-id",
  "tenant_id": "opaque-tenant-id",
  "meta_account_id": "123456789",
  "status": "ready",
  "proxy_url": "socks5://user:secret@host:port",
  "assignment_id": "opaque-egress-id"
}
```

`proxy_url` is intentionally returned here because the normal resolver is active-only while the first provider request must already use the tenant's egress. The fork must install this exact bootstrap proxy before performing provider I/O. This response is confidential and MUST NOT be logged verbatim.

A failed consume is terminal for that claim and MUST result in no Meta-bound request. A claim successfully consumed before provider authentication is one-shot even if Meta subsequently rejects the credentials; retry requires a new claim for the same connection.

### POST `/internal/v1/provisioning/bind-login`

Service-authenticated request after provider authentication has established the BridgeV2 login identity:

```json
{
  "connectionId": "opaque-connection-id",
  "metaAccountId": "123456789",
  "loginId": "bridgev2-login-id",
  "matrixOwnerMxid": "@owner:example.com"
}
```

Connection, Meta account and Matrix owner must still match the consumed provisioning identity. An existing different `mautrix_login_id`, or a login ID belonging to another connection, fails closed. This operation does not automatically activate production routing.

## Egress resolution

### GET `/internal/v1/egress/resolve`

Required query/context fields:

- `meta_account_id` when known;
- `login_id` when known;
- `reason`;
- `traffic_class`.

At least one stable account/login identity MUST be present. Anonymous global resolution is invalid for tenant traffic.

Successful response:

```json
{
  "proxy_url": "socks5://user:secret@host:port",
  "assignment_id": "opaque-id"
}
```

The response is confidential and MUST be sent only over the private service network. It MUST never be logged verbatim.

For `proxy_required`, missing assignment, disabled tenant/connection, disabled/unhealthy egress, missing secret or invalid configuration MUST produce a non-2xx response. Empty `proxy_url` is not a valid fail-closed response.

The normal resolver remains active-connection-only. Provisioning bootstrap does not weaken that invariant; it uses the one-time consume transaction described above.

Repeated calls for the same active connection MUST resolve to the same assignment until an explicit administrative reassignment.

## Management API minimum

The initial `/api/v1` surface supports tenants, Meta connections, egress profiles and Chatwoot bindings, explicit assignment operations, explicit connection activation and provisioning-claim issue/revoke. APIs use stable opaque internal IDs rather than MXIDs or provider IDs as primary keys.

State transitions MUST be explicit. In particular, successful provisioning yields `ready`, not `active`; production activation remains an explicit operation.

## Error model

JSON errors SHOULD use:

```json
{
  "error": {
    "code": "EGRESS_ASSIGNMENT_REQUIRED",
    "message": "safe operator-facing explanation",
    "request_id": "..."
  }
}
```

Codes are stable machine contracts; messages are not. Sensitive credentials, raw provisioning claims, cookies, provider responses and stack traces MUST never be returned except for the deliberate one-time claim issuance and confidential internal proxy responses described above.

Provisioning conflict codes include `PROVISIONING_CLAIM_INVALID`, `PROVISIONING_CLAIM_REVOKED`, `PROVISIONING_CLAIM_USED`, `PROVISIONING_CLAIM_EXPIRED`, `PROVISIONING_OWNER_MISMATCH`, `PROVISIONING_IDENTITY_CONFLICT`, `META_ACCOUNT_CONFLICT`, `MAUTRIX_LOGIN_CONFLICT`, `PROXY_REQUIRED_FOR_PROVISIONING`, and the existing egress fail-closed codes.

## Timeouts and retries

The resolver and provisioning consume/bind calls are on the Meta connection critical path and MUST have short bounded timeouts. The patched fork uses its dedicated control-plane HTTP client rather than ambient proxy environment configuration for these internal bearer-authenticated calls.

Mautrix MUST treat timeout as failure for `proxy_required`. The control plane MUST NOT perform unbounded proxy-provider network discovery synchronously in resolver or provisioning paths; eligibility reads persisted assignment/health state.

A provisioning consume MUST NOT be blindly retried after an indeterminate response because the claim may already have been consumed. Caller recovery is to inspect/restart the login with a newly issued claim, not to assume the previous claim is reusable.

Management and Chatwoot calls MAY use bounded retries only for operations known to be safe/idempotent or guarded by idempotency state.

## Health

`/health/live` proves the process is alive and MUST not depend on remote providers.

`/health/ready` proves local persistence is usable, migrations are current and required application initialization succeeded. It SHOULD NOT become unready merely because one tenant's external proxy or Chatwoot instance is degraded.

## Auditability

All state-changing management/provisioning operations MUST record actor, entity, safe before/after state and timestamp. Audit payloads MUST exclude raw claim secrets, Meta cookies and credential-bearing proxy URLs.

Internal egress resolutions SHOULD emit structured operational logs without storing raw secret-bearing proxy URLs.

## Required CI proofs

Contract tests MUST cover authentication, request validation, stable error codes, resolver sticky behavior, fail-closed behavior, disabled tenant/connection behavior, tenant boundary enforcement, secret redaction, timeout semantics, readiness behavior, digest-only claim persistence, forged/expired/used/revoked claim rejection, owner/tenant conflict rejection, direct-policy rejection, actual bootstrap-proxy transport and zero direct-provider sentinel traffic when provisioning fails.