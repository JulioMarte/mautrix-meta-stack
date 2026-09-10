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

`/internal/v1/*` MUST require service authentication using a header-carried secret. Query-string authentication is forbidden. Authentication failures return 401/403 without revealing whether a tenant, account or assignment exists.

Management API authentication may initially be protected by Cloudflare Access plus application-side state-changing request protection. Production evolution SHOULD introduce explicit operator identity/RBAC rather than treating possession of network access as full authority.

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

Repeated calls for the same active connection MUST resolve to the same assignment until an explicit administrative reassignment.

## Management API minimum

The initial `/api/v1` surface SHOULD support idempotent creation/update and retrieval for tenants, Meta connections, egress profiles and Chatwoot bindings, plus explicit assignment operations. APIs MUST use stable opaque internal IDs rather than using MXIDs or provider IDs as primary keys.

State transitions MUST be explicit; e.g. connection activation SHOULD validate required egress and Chatwoot bindings rather than merely toggling a boolean.

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

Codes are stable machine contracts; messages are not. Sensitive credentials, cookies, provider responses and stack traces MUST never be returned.

## Timeouts and retries

The resolver is on the Meta connection critical path and MUST have a short bounded timeout. Mautrix MUST treat timeout as failure for `proxy_required`. The control plane MUST NOT perform unbounded proxy-provider network discovery synchronously in the resolver path; health/exit-IP checks happen out of band and the resolver reads the last known eligible assignment.

Management and Chatwoot calls MAY use bounded retries only for operations known to be safe/idempotent or guarded by idempotency state.

## Health

`/health/live` proves the process is alive and MUST not depend on remote providers.

`/health/ready` proves local persistence is usable, migrations are current and required application initialization succeeded. It SHOULD NOT become unready merely because one tenant's external proxy or Chatwoot instance is degraded.

## Auditability

All state-changing management operations MUST record actor, entity, before/after state and timestamp. Internal egress resolutions SHOULD emit structured operational logs without storing raw secret-bearing proxy URLs.

## Required CI proofs

Contract tests MUST cover authentication, request validation, stable error codes, resolver sticky behavior, fail-closed behavior, disabled tenant/connection behavior, tenant boundary enforcement, secret redaction, timeout semantics and readiness behavior.