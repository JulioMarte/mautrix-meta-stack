# Meta Control Plane Documentation Index

Status: authoritative documentation map for `feature/meta-control-plane`

The branch is not ready for implementation unless these documents are coherent with each other. Where documents conflict, the more specific contract wins only if the conflict is explicitly resolved in the branch plan or ADR; silent contradictions are not acceptable.

## Core documents

- `meta-control-plane-branch-plan.md` — branch purpose, product hypothesis, architecture, phases, non-goals, Definition of Done and stop conditions.
- `meta-control-plane-ci-acceptance.md` — what must be proven automatically before any capability can be called complete.
- `mautrix-meta-fork-delta.md` — exact scope and maintainability contract for the account-aware egress patch.
- `meta-control-plane-data-model.md` — entities, identity boundaries, persistence, migrations, referential invariants and portability expectations.
- `meta-control-plane-internal-api.md` — public/internal API split, resolver contract, authentication, errors, readiness and timeout semantics.
- `meta-control-plane-event-contracts.md` — normalized Matrix/Chatwoot messaging model, routing, idempotency, attachments and loop prevention.
- `phase5-chatwoot-matrix.md` — concrete Phase 5 Chatwoot webhook authentication, exact routing, Matrix send/idempotency, attachments and CI gate.
- `meta-control-plane-threat-failure-model.md` — trust boundaries, leakage/cross-tenant threats, dependency failures, crash semantics and required fault injection.
- `meta-control-plane-deployment-operations.md` — Coolify topology, state, secrets, startup, backup/restore, upgrade, smoke tests and rollback.
- `meta-control-plane-onboarding-identity-binding.md` — pre-Meta bootstrap identity, provisioning claims, first-login binding, re-login and conflict semantics.
- `meta-control-plane-matrix-adapter.md` — Matrix service identity, ingestion mechanism boundary, room attribution, checkpoints, restart and outbound send semantics.
- `meta-control-plane-chatwoot-tenancy.md` — Chatwoot account/inbox tenancy, contact/conversation identity, webhook routing, migration and retry semantics.

## Normative invariants spanning all documents

1. Tenant identity is not Matrix identity and is not Meta identity.
2. A single mautrix process may host multiple Meta logins, but each production connection has an explicit tenant-scoped egress assignment.
3. `proxy_required` always fails closed; direct Contabo fallback is forbidden.
4. Sticky assignment means reconnect does not imply proxy rotation.
5. Login, messaging, media and E2EE-relevant traffic must satisfy the same tenant egress isolation guarantee before production readiness is claimed.
6. The control plane never stores Meta session cookies.
7. Proxy and Chatwoot credentials are secrets; canonical raw secret-bearing URIs/tokens are not persisted or rendered.
8. Matrix <-> Chatwoot routing is driven by persisted tenant-scoped conversation bindings.
9. Duplicate and replayed external events must not duplicate downstream side effects.
10. No capability is complete until deterministic CI proves the claim on the exact candidate commit.
11. Manual Element/Chatwoot success is diagnostic evidence, not acceptance evidence.
12. Feature-branch work must not touch deployment branch `main` until a deliberate green integration checkpoint.
13. A Meta login must be associated with exactly one pre-created `meta_connection` before the first Meta-bound request; Matrix identity alone is not sufficient when one user can own multiple Meta logins.
14. Matrix room names/display names are never routing authority; room attribution requires persisted stable identifiers and verifiable bridge/Matrix metadata.
15. Chatwoot inbox/conversation IDs are always interpreted in their tenant + installation/account context; numeric IDs alone are not security boundaries.
16. Unknown, ambiguous or conflicting identity/binding information fails closed rather than being guessed or overwritten.
17. Chatwoot webhook signing secrets are distinct from Chatwoot API credentials and use a dedicated secret-reference namespace.

## Phase 0 exit review

Before Phase 1 starts, reviewers should be able to answer unambiguously:

- What is a tenant, Meta connection and provider login?
- How is a `meta_connection` created before Meta authentication?
- How is the first login bound to exactly one connection before the first Facebook request?
- What prevents one Matrix user with multiple Meta accounts from being routed ambiguously?
- What identifier is used before the first Facebook request?
- How is an egress assignment selected and when may it change?
- What happens if the resolver or proxy fails?
- Can any Meta traffic bypass the assigned egress?
- Where do proxy/Chatwoot secrets live?
- How does the control plane ingest Matrix events?
- How is a Matrix room attributed to exactly one Meta connection?
- How does Matrix event consumption recover across restart without duplicate delivery?
- How is a remote Meta contact/thread mapped to Chatwoot contact/source/conversation objects?
- What Chatwoot account/inbox isolation model is being used?
- How are duplicate Matrix events and Chatwoot webhooks handled?
- What happens across process restart?
- Which endpoints are public versus private?
- How does an operator disable or reassign a connection?
- What happens to existing conversations when a Chatwoot inbox binding changes?
- What tests prove each claim?
- What conditions force architecture reassessment instead of continued patching?

If any answer requires guessing from implementation instead of these documents, Phase 0 is incomplete.

## Deliberately deferred specifications

The following do not block Phase 1 but MUST be specified before their corresponding production capability is claimed complete:

- concrete proxy-provider adapter(s) and credential provisioning workflow;
- exact Matrix ingestion transport choice (`/sync`/sliding sync vs application-service style) before Phase 4 acceptance;
- exact bridge metadata/state signal used to prove room attribution before Phase 4 acceptance;
- exact deterministic Chatwoot source/contact identity derivation after validating the deployed API behavior;
- production operator RBAC beyond the initial protected admin surface;
- data retention durations once message volume and compliance requirements are known;
- PostgreSQL migration timing/thresholds;
- full OpenTelemetry/metrics backend;
- billing, quotas and tenant self-service onboarding.

The Chatwoot webhook authentication mechanism is no longer deferred: Phase 5 fixes the supported contract to Chatwoot's timestamped HMAC-SHA256 signature over the raw request body, with a binding-specific secret configured under `env:CHATWOOT_WEBHOOK_*`.

Deferral means these are explicitly not assumed. Any implementation depending on one of them must first turn the deferred item into a concrete reviewed contract.
