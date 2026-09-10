# Meta Control Plane Threat and Failure Model

Status: normative for `feature/meta-control-plane`

## Trust boundaries

The system has distinct trust zones:

1. operator/admin browser;
2. public Chatwoot webhook ingress;
3. internal Coolify service network;
4. Matrix/Synapse;
5. patched mautrix-meta;
6. residential proxy provider;
7. Meta;
8. persistent SQLite volume and backups.

No trust boundary may rely solely on obscurity or an unguessable URL.

## Assets to protect

Critical assets include tenant routing identity, Meta account association, Matrix room mapping, Chatwoot credentials, proxy credentials, internal resolver tokens, audit history and the guarantee that `proxy_required` traffic never uses direct Contabo egress.

Meta session cookies are especially sensitive but remain owned by mautrix-meta; the control plane MUST NOT copy them into its database.

## Primary failure/security cases

### Cross-tenant routing

A message, conversation binding, proxy assignment or Chatwoot credential from tenant A must never be selected for tenant B. All lookups require tenant/connection scope and tests must use colliding-looking fixture identifiers to catch accidental global lookups.

### Direct-egress leakage

For `proxy_required`, any missing resolver data, proxy outage, malformed proxy, startup race or reconnect failure must stop the Meta-bound operation rather than use host egress. A CI sentinel endpoint MUST prove direct fallback does not occur.

### Credential leakage

Logs, stack traces, admin pages, metrics, audit JSON and API errors MUST redact proxy passwords, Chatwoot tokens, internal tokens and Meta cookies. CI uses canary secrets and scans produced output.

### Forged internal requests

`/internal/v1/*` requires service authentication. A caller without valid credentials cannot retrieve proxy URIs or mutate routing state.

### Forged/replayed Chatwoot webhooks

Webhook ingress must use the strongest validation available for the deployed Chatwoot mechanism plus idempotency records. Replays may be accepted at HTTP level but MUST NOT reproduce downstream effects.

### Admin compromise/CSRF

State-changing admin operations require authenticated sessions and CSRF-safe semantics. Egress reassignment and disabling/enabling connections are privileged actions and must be audited.

### Stale proxy health

Health is advisory unless policy explicitly requires freshness. Resolver behavior must be deterministic: an assignment marked unusable under `proxy_required` fails closed. Health workers cannot silently replace sticky egress without an audited administrative/policy transition.

### Dependency outages

Control plane DB unavailable -> readiness false; no routing side effects.
Chatwoot unavailable -> retryable delivery state.
Matrix unavailable -> retryable state.
Proxy resolver unavailable to mautrix -> fail required connection path.
Meta unavailable -> mautrix handles protocol retry, but egress identity must remain stable.

### Crash between side effect and persistence

Every external side effect must be designed for ambiguous completion. `processed_events` plus downstream correlation/reconciliation must prevent a restart from multiplying messages.

### SQLite corruption or volume loss

The service must fail readiness on unreadable/corrupt persistence. Backup/restore procedures are required before production designation. A restored database must retain tenant, connection, egress assignment, conversation binding and idempotency state.

### Upstream mautrix drift

A new upstream tag may alter proxy/media/E2EE behavior. Upgrades are blocked until fork-delta tests and egress-isolation tests pass against the new tag.

## Privacy/data minimization

Do not persist raw Meta cookies. Avoid retaining complete raw webhooks or message bodies solely for diagnostics. Structured logs should prefer IDs and outcomes over content. Backups inherit the sensitivity of the SQLite database and must be access-controlled.

## Abuse considerations

The platform is intended to unify accounts legitimately controlled by tenants. It must not include features for account takeover, credential harvesting, bypassing Meta authentication challenges, or evading enforcement through uncontrolled proxy rotation. Egress assignment is for stable tenant isolation, not churn.

## Required fault injection

CI must inject at least: resolver timeout, missing assignment, dead proxy, malformed proxy response, Chatwoot 5xx/timeout, Matrix send failure, duplicate Matrix event, duplicate Chatwoot webhook, process restart during retryable delivery, disabled tenant, cross-tenant identifier collision and secret-canary exposure attempt.

## Production-only smoke checks

CI cannot prove third-party real-world behavior such as whether Meta accepts a given residential ASN, real DNS/TLS routing or an actual provider's exit IP at deployment time. Production smoke tests must verify those separately, but they do not replace deterministic CI proofs.