# Meta Control Plane Deployment and Operations Contract

Status: normative for `feature/meta-control-plane`

## Target deployment topology

The production-oriented Compose topology is:

```text
public HTTPS
   |
   +-> Synapse
   +-> control-plane admin/API (only required public routes)
   +-> Chatwoot webhook ingress

private Coolify network
   |
   +-> mautrix-meta
   +-> control plane internal API
   +-> SQLite persistent volume
```

The egress resolver and bridge management paths MUST remain internal-only.

## Coolify/GitHub deployment rule

`main` is the deployment branch and represents the currently human-accepted working deployment state. Feature work MUST remain off `main` until its required CI gates pass, real-environment requirements are understood, and a human owner explicitly approves promotion of an identified candidate SHA.

A merge to `main` is deployment-triggering and MUST NOT be performed automatically by an agent, CI workflow, bot, or unattended release process. Green `dev` CI is necessary evidence, not promotion authorization.

The default rule is therefore:

```text
feature/fix/docs -> dev -> automated evidence -> controlled staging/manual review
                                      |
                                      +-> human explicitly approves exact SHA
                                                      |
                                                      v
                                                    main
```

Until the explicit human approval step occurs, `main` remains unchanged even when `dev` is technically deployable.

No deployment procedure may require ad-hoc SSH mutation of container state as the normal path. Required initialization, migrations and configuration belong in repository-controlled Compose/services/scripts plus Coolify secrets/environment.

## Persistent state

The control-plane SQLite database requires a named persistent volume. Container recreation must not reset tenant configuration, egress assignments, conversation bindings, processed events or audit history.

The deployment must distinguish immutable application image from mutable state volume.

## Secrets

Runtime secrets live in Coolify/environment or a later dedicated secret backend. At minimum this includes Chatwoot API credentials, proxy secret material and internal resolver authentication tokens.

Secrets MUST NOT be committed to Git, embedded in Docker images, included in Compose defaults, emitted by bootstrap jobs or copied into CI artifacts.

## Startup ordering

Control plane startup MUST:

1. validate required non-secret configuration;
2. open persistence;
3. apply migrations;
4. initialize repositories/services;
5. become ready only after local invariants hold.

Mautrix may start independently for basic Matrix service availability, but production Meta connections using `proxy_required` must not proceed unless the resolver is reachable and returns a valid assignment.

## Backup and restore

Before production designation there must be a tested backup path for the control-plane data volume. Restore proof must show the system recovers tenant records, Meta connection IDs, sticky egress assignments, Chatwoot/conversation bindings, processed-event state and audit history.

The repository CI includes a cold-volume recovery gate that stops the control plane, archives the complete `/data` volume state, destroys the original disposable volume, restores into a fresh volume, starts through the normal initialization path, and verifies the required identity/routing/idempotency/audit state. This deterministic CI proof validates the repository-controlled recovery procedure; production operators still need to validate where real backups are stored, retained and protected.

Backups contain sensitive operational metadata and must be protected accordingly.

## Upgrade strategy

Application upgrade:

- CI validates migrations from the current released schema;
- backup is taken/available;
- deploy runs migrations automatically;
- readiness remains false on migration failure;
- rollback must account for whether the schema migration is backward compatible.

Mautrix fork upgrade additionally follows `mautrix-meta-fork-delta.md` and cannot ship until upstream-delta and egress-isolation lanes pass.

## Operational lifecycle

A tenant connection should move through explicit states such as `draft -> ready -> active`, with `degraded`, `blocked`, `disabled` as operational states. Activation must verify required bindings and egress policy rather than simply setting a boolean.

Proxy reassignment is an explicit audited operation. Automatic reconnect must reuse the existing assignment and must not rotate egress.

Disabling a tenant or Meta connection must stop future routing as quickly as practical without deleting historical bindings/audit records.

## Observability

Structured logs must carry request/correlation IDs and applicable tenant/connection identifiers while redacting all secrets. Readiness/liveness are separate from per-tenant dependency health.

Operational dashboards may later expose counts of active/degraded connections, resolver failures, routing failures, duplicate suppression and proxy-health failures, but metrics MUST never include raw credentials or message content.

## Production smoke verification

After a release that changes networking/routing, verify externally:

- public Synapse endpoint still responds;
- control-plane public routes are correctly protected;
- internal resolver is not Internet-accessible;
- assigned real proxy reports the expected exit country/IP;
- at least two controlled real Meta logins can coexist in one mautrix process when the branch claims final multi-tenant production readiness;
- each controlled Meta login uses its intended real egress and no protected path uses host/direct egress;
- one controlled Meta message reaches the intended Chatwoot inbox and a reply returns to the intended Meta thread;
- restart/redeploy preserves the live routing identities being evaluated;
- no unexpected direct-host egress is observed.

These smoke checks complement CI; they do not substitute for it. CI test doubles cannot prove real Meta account acceptance, provider-specific residential proxy behavior, public exit IP, Coolify/DNS exposure, or the absence of host egress in the actual deployment environment.

A failure or unperformed item in this section means production readiness remains unproven even if all repository CI is green.

## Rollback/kill switch

Operators need a fast way to disable one connection or all Meta routing without destroying state. A bad deployment should be rollbackable at the application/container level, but no rollback may silently bypass `proxy_required` or discard newer routing/idempotency state.

Before any human-approved `main` promotion, the operator must understand the rollback target and confirm that restoring the prior application state will not require destructive database rollback or discard state written by the candidate.

## Required CI proofs

CI MUST cover Compose validity, fresh startup, migrations, restart persistence, readiness semantics, absence of required public exposure for internal services where statically testable, backup/restore at least once before production designation, and exact candidate-SHA integration tests.

Passing these proofs makes the candidate eligible for human staging evaluation. It does not authorize or imply a merge to `main`.
