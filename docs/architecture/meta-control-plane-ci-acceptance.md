# Meta Control Plane CI Acceptance Contract

Status: **Normative**  
Branch: `feature/meta-control-plane`  
Applies to: all implementation phases and the final Definition of Done

## 1. Governing rule

No capability in this branch may be described as **implemented**, **working**, **ready**, **complete**, **production-ready**, or **safe to merge** merely because it works manually in Element, Chatwoot, a local shell, or a developer environment.

A capability is considered complete only when its required behavior is exercised automatically by CI and the corresponding required checks pass on the exact commit being evaluated.

Manual verification may supplement CI. It may not replace CI.

The burden of proof is on the implementation. If a property cannot be verified automatically, the branch must either add the instrumentation/test seam required to verify it or explicitly classify that property as not yet proven.

## 2. Required CI layers

The CI system must contain independent layers rather than a single broad smoke test.

### 2.1 Static and build checks

CI must verify, as applicable:

- Bun dependency installation from the lockfile;
- TypeScript type checking;
- lint/static analysis;
- unit-test execution;
- production build/container build;
- Docker Compose rendering/validation;
- migration syntax and schema initialization;
- no accidental dependency on local developer files.

### 2.2 Domain unit tests

CI must deterministically verify:

- tenant boundaries;
- Meta connection ownership;
- sticky egress assignment;
- `proxy_required` fail-closed behavior;
- explicit direct-egress policy behavior;
- secret redaction;
- audit creation for security-sensitive configuration changes;
- processed-event uniqueness/idempotency;
- conversation-binding resolution;
- Chatwoot echo suppression;
- invalid state transitions are rejected.

### 2.3 Contract tests

CI must prove the contracts between components, not merely mock internal methods.

Required contracts include:

- patched mautrix -> Control Plane egress resolver request;
- account identity propagation before the first Meta request;
- resolver authentication;
- resolver response decoding;
- missing/unhealthy egress rejection for `proxy_required`;
- Matrix event -> normalized message;
- normalized message -> Chatwoot request;
- Chatwoot webhook -> normalized message;
- normalized outbound message -> Matrix send;
- retry/idempotency semantics across remote failures.

### 2.4 Real-container integration tests

Where wiring is part of the claimed behavior, CI must run real containers rather than replacing every boundary with mocks.

At minimum, the integration lane must be able to start the pinned topology required by the tested phase, including:

```text
Synapse
mautrix-meta fork
Meta Control Plane
SQLite persistent volume
Chatwoot-compatible test double or real disposable Chatwoot when practical
proxy/egress test fixtures
```

The topology must start from disposable clean state. Tests must not depend on data left by previous jobs.

### 2.5 Fault-injection tests

Production claims require failure-path evidence.

CI must inject and verify at least:

- egress resolver unavailable;
- assigned proxy unavailable;
- resolver returns malformed data;
- resolver returns unauthorized/forbidden;
- Chatwoot API timeout;
- Chatwoot API error after an ambiguous/possibly successful request;
- duplicate Chatwoot webhook;
- duplicate Matrix event;
- Control Plane restart;
- mautrix reconnect;
- missing conversation binding;
- disabled tenant/connection.

A failure must not produce cross-tenant routing, silent direct egress, duplicate user-visible messages, or secret leakage.

## 3. Egress isolation is a CI security property

The most important security assertion of this branch is not a unit test of a lookup function. CI must prove observable network behavior.

For two independent connections A and B, CI must establish two distinct controlled egress endpoints and prove:

```text
connection A -> egress A
connection B -> egress B
```

The proof must cover every Meta traffic class that the branch claims to isolate, including at least:

```text
login
messaging/connect/reconnect
media
E2EE-relevant traffic
```

For a `proxy_required` connection, CI must additionally prove that when the assigned egress is unavailable the connection fails instead of reaching a direct-network sentinel representing the Contabo host path.

A test that only asserts `proxy_url == expected` is insufficient. The final integration/security lane must observe which egress endpoint actually received the connection.

## 4. Multi-tenant routing proof

CI must create at least two independent tenants and two independent Meta connections with deliberately different identifiers, egress assignments, Matrix rooms, and Chatwoot inboxes.

The test must prove both directions concurrently or in an interleaved sequence designed to reveal accidental global state:

```text
Meta A -> Matrix room A -> Chatwoot inbox A
Meta B -> Matrix room B -> Chatwoot inbox B

Chatwoot A -> Matrix room A -> Meta thread A
Chatwoot B -> Matrix room B -> Meta thread B
```

CI must fail if any A event appears in a B destination or vice versa.

Testing only one tenant is not evidence of multi-tenancy.

## 5. Persistence and restart proof

Before final acceptance, CI must prove that durable identity survives restart/redeploy of disposable containers while preserving the same persisted volume/database state.

At minimum it must verify:

- tenant IDs remain stable;
- Meta connection bindings remain stable;
- egress assignments remain sticky;
- conversation bindings remain usable;
- processed-event/idempotency records survive;
- replay after restart does not create duplicate Chatwoot/Matrix messages.

## 6. Secret-leakage checks

CI must use unmistakable synthetic canary secrets for:

- proxy password;
- Chatwoot API token;
- internal resolver token.

After exercising success and failure paths, CI must scan captured application logs and rendered admin/API responses for those exact canary values.

The check must fail if a raw secret is found where it is not explicitly required as a private machine-to-machine credential response.

Repository secret scanning should also reject committed real-looking credentials where practical.

## 7. mautrix fork verification

The fork delta is itself a tested artifact.

CI must:

- pin the exact upstream mautrix-meta version/commit the fork is based on;
- exercise account-specific proxy-context propagation;
- exercise login-time identity extraction before the first Meta network request;
- exercise reconnect behavior;
- exercise all traffic classes the fork modifies;
- detect accidental regression to connector-global proxy behavior;
- keep a reproducible patch/diff against the pinned upstream base.

A fork that compiles but whose account context is not exercised in CI is not acceptable.

## 8. Phase-gate policy

Each implementation phase has its own CI acceptance gate. Later phases may not be reported complete while an earlier required gate is red or missing.

### Phase 1 gate — Control Plane skeleton

Must prove automatically:

```text
service starts
migrations run from empty state
schema is idempotently reusable
health/live works
health/ready reflects dependency readiness
repositories persist and retrieve tenant/config records
admin/API authentication rejects unauthorized access
```

### Phase 2 gate — Egress resolver

Must prove automatically:

```text
account resolves to assigned egress
same account repeatedly resolves to same egress
two accounts can resolve to different egresses
proxy_required fails closed
inactive tenant/connection is rejected
unhealthy assignment is rejected
secrets are redacted
audit records are written
```

### Phase 3 gate — mautrix fork

Must prove automatically:

```text
account ID reaches resolver on first login path
login cannot contact Meta-direct sentinel under proxy_required
connect/reconnect preserve account egress
media/E2EE claimed paths preserve account egress
resolver failure stops the protected path
fork remains buildable against pinned upstream base
```

### Phase 4 gate — Matrix -> Chatwoot

Must prove automatically:

```text
Matrix event becomes normalized message
correct tenant/connection is selected
correct Chatwoot inbox/conversation is selected
duplicate Matrix event creates one Chatwoot side effect
attachments supported by the phase preserve routing identity
```

### Phase 5 gate — Chatwoot -> Matrix

Must prove automatically:

```text
webhook authentication/validation works
correct conversation binding is selected
correct Matrix room is targeted
echo is suppressed
duplicate webhook creates one Matrix side effect
ambiguous Chatwoot/network retries do not duplicate user-visible messages
```

### Phase 6 gate — Multi-tenant production proof

Must prove automatically:

```text
two tenants operate simultaneously
two distinct egresses are observed
two distinct Chatwoot routes are observed
bidirectional traffic stays tenant-correct
restart preserves routing identity
fault injection fails safely
no protected path reaches direct-egress sentinel
no synthetic secret appears in captured logs/UI responses
```

## 9. Required-check semantics

The final PR to `main` must not be merged on the basis of an aggregate "tests passed" message alone.

CI should expose named checks whose intent is obvious from the branch contract, for example:

```text
control-plane / quality
control-plane / domain
control-plane / contracts
control-plane / integration
control-plane / egress-isolation
control-plane / multi-tenant-routing
control-plane / fault-injection
control-plane / secret-leakage
mautrix-fork / upstream-delta
```

The exact workflow naming may evolve, but equivalent evidence must remain visible and attributable.

A skipped required security/integration check is not green. It is unproven and must block a readiness claim.

## 10. CI realism and test doubles

Tests should be deterministic and must not require real customer Meta accounts, production Chatwoot data, or paid residential proxies for every PR.

Use purpose-built protocol/network test doubles that record observable connection metadata for routine CI. Real external-service validation may be run as a separate controlled acceptance lane when credentials/services are required.

However, mocks must not erase the property being tested. In particular:

- egress tests must observe actual network destination selection;
- container wiring tests must run actual containers;
- persistence tests must use the actual configured database adapter;
- mautrix-context tests must execute the patched code path;
- routing tests must exercise real HTTP/Matrix adapter boundaries where practical.

## 11. Evidence required before saying "ready"

Before any final readiness statement, the agent/engineer must inspect the CI result for the exact candidate commit and confirm that every required lane completed successfully.

The readiness report must identify:

```text
candidate commit SHA
required CI checks
pass/fail/skip status
what each critical integration/security test actually proved
known properties that remain manual or untested
```

If a required check is missing, skipped, flaky, or was run against a different commit, the correct status is **not ready**.

## 12. Relationship to manual production validation

CI is the prerequisite, not the entire production rollout strategy.

After CI is green, a controlled deployment may still require real-environment smoke checks for DNS, Coolify networking, actual proxy-provider behavior, Meta account acceptance, and Chatwoot installation-specific configuration.

Those production checks can discover environmental differences, but they must not be used to excuse missing deterministic CI coverage of behavior we control.

The sequence is therefore:

```text
implementation
-> deterministic automated tests
-> all required CI lanes green on exact commit
-> review evidence
-> only then declare branch technically ready
-> controlled deployment
-> production smoke/acceptance verification
```

This ordering is mandatory for this branch.
