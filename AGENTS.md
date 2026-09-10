# Repository Working Contract

This file is a concise execution guide for humans and coding agents working on `feature/meta-control-plane`. The authoritative specifications remain under `docs/architecture/`; this file does not override them.

## Branch and deployment discipline

- Do implementation work for the Meta Control Plane on `feature/meta-control-plane`.
- Do not move feature work to `main` until the required phase gate is green on the exact candidate commit.
- Treat `main` as deployment-triggering state.
- Never describe a capability as complete, production-ready, or safe to merge unless the corresponding CI acceptance contract is satisfied.

## Source-of-truth reading order

Before changing architecture or implementation, read:

1. `docs/architecture/meta-control-plane-documentation-index.md`
2. `docs/architecture/meta-control-plane-branch-plan.md`
3. the most specific contract for the subsystem being changed
4. `docs/architecture/meta-control-plane-ci-acceptance.md`
5. `docs/architecture/meta-control-plane-threat-failure-model.md`

Silent contradictions between contracts are not acceptable. Resolve the contract first rather than guessing in code.

## Current implementation sequence

Phase 0 documentation/contracts are the established baseline. Implementation proceeds in the branch-plan order:

1. Phase 1 — Control-plane skeleton
2. Phase 2 — Egress resolver
3. Phase 3 — Minimal mautrix-meta fork
4. Phase 4 — Matrix to Chatwoot
5. Phase 5 — Chatwoot to Matrix
6. Phase 6 — Multi-tenant proof

Do not pull later-phase behavior forward if it weakens isolation, idempotency, testability, or maintainability.

## mautrix-meta upstream contract

The fork baseline is exactly:

- repository: `mautrix/meta`
- tag: `v0.2607.0`
- commit: `ed37c9e6ce47e83dc75b9abea7b636302715b9bc`

The fork exists only for account-aware egress resolution and propagation of the tenant-specific egress across required Meta traffic classes. Keep the delta small, mechanically reviewable, and rebaseable. Do not put Chatwoot, tenant business logic, CRM behavior, or control-plane persistence into the mautrix fork.

## Non-negotiable invariants

- Tenant identity, Matrix identity, Meta account identity, and mautrix `UserLogin.ID` are separate concepts.
- A Meta login must bind to exactly one pre-created `meta_connection` before the first Meta-bound request.
- `proxy_required` fails closed. Direct Contabo fallback is forbidden.
- Reconnect does not rotate egress; reassignment is explicit and audited.
- Login, messaging, media, and E2EE-relevant traffic must satisfy the same tenant egress-isolation claim before production readiness.
- The control plane never stores Meta session cookies.
- Raw proxy and Chatwoot secrets are not persisted canonically, logged, rendered, or committed.
- Matrix/Chatwoot routing uses persisted tenant-scoped stable identifiers, never display names or ambiguous numeric IDs alone.
- Duplicate/replayed external events must not duplicate user-visible downstream side effects.
- Unknown or conflicting identity/binding state fails closed rather than being guessed.

## Implementation quality bar

For each phase:

- add or update deterministic tests with the implementation;
- preserve repository/service boundaries so SQLite can later be replaced by PostgreSQL;
- keep state transitions explicit;
- keep internal endpoints private and authenticated;
- keep liveness independent of remote providers and readiness tied to local initialization/migration correctness;
- use structured logs with request/correlation IDs and tenant/connection context where applicable;
- test failure paths, not only success paths.

If a required property cannot be tested, first add the instrumentation or test seam needed to make it observable.

## Stop conditions

Stop implementation and reassess architecture instead of layering patches when any of these becomes true:

- the mautrix fork requires broad protocol rewrites or unrelated changes;
- tenant-specific egress cannot be guaranteed for all traffic classes we claim to isolate;
- stable room/account attribution requires guessing from names or mutable presentation data;
- cross-tenant integrity cannot be enforced cleanly at service/repository boundaries;
- a production path would need to bypass `proxy_required` to remain operational;
- a deferred contract becomes necessary for the current phase but is still unspecified.
