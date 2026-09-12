# Controlled Staging Acceptance Runbook

Status: normative human-evidence checklist for the final pre-production evaluation of the Meta Control Plane.

This runbook covers the assertions that repository CI cannot honestly prove with fixtures: real Meta account acceptance, real residential egress, public exit identity, Coolify/DNS/TLS behavior, and real backup operations. It does not authorize promotion to `main`.

## Entry conditions

Do not begin staging unless all of the following are true:

- the exact `dev` candidate SHA is recorded before the test starts;
- the complete required repository acceptance matrix for that SHA is green;
- the candidate contains no uncommitted or out-of-band production changes;
- at least two disposable Meta/Facebook test accounts are available;
- at least two distinct approved egress endpoints are available and each has a known expected public IP and country;
- separate tenant, Matrix, Meta connection and Chatwoot identities are prepared for A and B;
- secrets are supplied only through Coolify/environment or the approved runtime secret mechanism;
- the operator has a rollback target and a current backup of the staging state before destructive/restart tests.

Record the candidate as:

```text
candidate_sha=<exact dev SHA>
started_at=<UTC timestamp>
operator=<human operator>
environment=<staging environment identifier>
```

If the SHA changes, stop. Evidence collected against different commits must not be combined into one acceptance result.

## Required topology

The staging deployment must exercise the real intended boundaries:

```text
public HTTPS
   |
   +-> Synapse
   +-> protected control-plane public surface
   +-> Chatwoot webhook ingress

private application network
   |
   +-> mautrix-meta
   +-> control-plane internal API / egress resolver
   +-> persistent control-plane data volume

Meta connection A -> residential egress A
Meta connection B -> residential egress B
```

The internal egress resolver and bridge-management endpoints must not be reachable from the public Internet.

## Evidence record

For every required check, record:

```text
check_id
candidate_sha
started_at / finished_at
result = PASS | FAIL | NOT_RUN
observable evidence
relevant tenant_id / connection_id when applicable
expected egress identity
observed egress identity
log/artifact reference with secrets removed
operator notes
```

Do not paste Meta cookies, access tokens, proxy passwords, Chatwoot tokens, complete credential-bearing proxy URIs, or other raw secrets into the evidence record.

## Stage A — deployment and exposure

### STG-01 Candidate identity

Prove the running control-plane and patched mautrix artifacts came from the recorded candidate SHA. Record image/tag/digest or build provenance sufficient to identify the deployed artifact.

Pass condition: deployed artifacts are attributable to exactly the candidate under evaluation.

### STG-02 Public/private boundary

From an external network, verify:

- Synapse is reachable only through the intended public route;
- public control-plane routes are protected as designed;
- Chatwoot webhook ingress is reachable only where intended;
- `/internal/v1/*`, the egress resolver and mautrix bridge-management paths are not Internet-accessible;
- mautrix application-service port is not directly published.

Pass condition: no internal-only route is externally reachable.

### STG-03 Fresh/redeploy startup

Perform a normal Coolify deploy/redeploy using repository-controlled initialization. Do not repair state by ad-hoc SSH mutation.

Pass condition: migrations/startup complete normally, liveness becomes healthy, readiness becomes healthy only after local invariants hold, and persisted identities survive redeploy.

## Stage B — real egress isolation

Use two distinct real connections A and B with `proxy_required`.

### STG-10 Initial login egress

For each connection, begin a real Meta login while observing the public exit at the controlled proxy boundary.

Pass conditions:

- A's first provider-bound traffic exits through egress A;
- B's first provider-bound traffic exits through egress B;
- neither performs provider-bound traffic through the host/Contabo public IP.

### STG-11 Connected messaging/reconnect egress

After successful login, exercise connect, disconnect/reconnect and normal message traffic for A and B.

Pass conditions:

- A remains on egress A;
- B remains on egress B;
- reconnect does not rotate assignments;
- no direct-host fallback is observed.

### STG-12 Media/avatar traffic

Exercise provider operations that fetch or upload media/avatar content for both connections.

Pass condition: observable network traffic for each account continues through its assigned egress with no cross-account or direct-host leakage.

### STG-13 E2EE-relevant traffic

Exercise the available E2EE-relevant provider path for both controlled accounts.

Pass condition: the path uses the assigned account egress or fails closed; it must not bypass to the host network.

### STG-14 Assigned-proxy failure

Make egress A unavailable while connection A remains `proxy_required`. Keep direct host connectivity otherwise possible so fallback would be observable.

Pass conditions:

- A fails/degrades/blocks rather than reaching Meta directly;
- B remains isolated on egress B;
- restoring A's assigned egress allows recovery without changing the persisted assignment unless an operator explicitly reassigns it.

## Stage C — real multi-tenant routing

### STG-20 Meta A -> Chatwoot A

Send a real message into Meta account A.

Pass condition: it is attributed to connection A, reaches the intended Matrix room A and creates/uses the intended Chatwoot tenant/account/inbox A route only.

### STG-21 Meta B -> Chatwoot B

Repeat for B while A is active.

Pass condition: B reaches only B destinations and no A identifiers or side effects are used.

### STG-22 Chatwoot A -> Meta A

Reply as a human agent in Chatwoot A.

Pass condition: the reply targets the persisted Matrix room/thread for A and reaches the correct Meta conversation through egress A exactly once.

### STG-23 Chatwoot B -> Meta B

Repeat for B in an interleaved sequence with A.

Pass condition: no cross-tenant routing, duplicate user-visible delivery or ambiguous numeric-ID routing occurs.

### STG-24 Restart persistence

Restart/redeploy the control plane and mautrix using the same persistent state, then repeat one inbound and one outbound message for A and B.

Pass conditions:

- tenant IDs, Meta connection IDs, mautrix login bindings, Matrix room attribution, Chatwoot conversation bindings and egress assignments remain stable;
- previously processed events are not duplicated;
- reconnect still uses the original assigned egress.

## Stage D — migration and disable semantics

### STG-30 Chatwoot binding migration

For one controlled tenant, establish an existing conversation on binding A, move the connection's current binding to B, then exercise both the historical thread and a new thread.

Pass conditions:

- historical thread remains routed through its persisted binding A;
- new thread uses current binding B;
- a webhook on historical A can still target the original Matrix room;
- numeric identifier collisions do not allow B or another tenant to claim A's historical conversation.

### STG-31 Historical binding disabled

Disable historical binding A and attempt new delivery for its historical thread.

Pass condition: delivery fails terminally/fail-closed with no silent fallback to B.

### STG-32 Connection disable and egress release

Disable a controlled connection and verify that routing stops. If the same egress profile is then deliberately assigned to another live connection, attempt to reactivate the original connection without changing its assignment.

Pass conditions:

- disabled connection produces no new routing;
- released egress may be deliberately reused;
- reactivation fails if that profile is now reserved by another live connection until an operator explicitly reassigns egress.

## Stage E — real backup and restore operations

Repository CI proves the recovery algorithm on disposable Docker volumes. Staging must prove the actual operational mechanism.

### STG-40 Backup creation

Create an off-container/off-volume backup of the complete control-plane persistent state using the intended production mechanism.

Record:

- actual volume/storage identifier;
- backup destination class/location (not credentials);
- encryption-at-rest method;
- access-control owner/role;
- retention policy;
- timestamp and backup identifier.

Pass condition: the backup is stored outside the failure domain of the source volume and is protected according to the intended operational policy.

### STG-41 Destructive restore drill

Stop the control plane, preserve the backup evidence, destroy or replace the staging data volume, restore into a clean replacement volume, and start through the normal initialization path.

Pass conditions:

- tenant/configuration identity is recovered;
- Meta connection and mautrix login bindings are recovered;
- sticky egress assignments are recovered;
- Chatwoot/conversation bindings are recovered;
- Matrix room attribution and sync checkpoint state are recovered;
- processed-event/idempotency and audit history are recovered;
- replay of a previously processed event does not create a duplicate downstream side effect.

### STG-42 Restore permissions

Prove the documented operator/service identity can restore without broadening runtime permissions beyond what is required.

Pass condition: restore is operationally executable by the intended authorized role and does not require undocumented root/SSH mutation as the normal procedure.

## Stage F — secret and operational review

### STG-50 Secret exposure

Review application logs, Coolify deployment logs, captured evidence and public/admin responses generated during the run.

Pass condition: no raw proxy password, Meta cookie/session material, Chatwoot token, internal resolver token or complete credential-bearing proxy URI is exposed.

### STG-51 Failure visibility

Review at least one induced provider/proxy/routing failure.

Pass condition: operators can identify the affected tenant/connection and failure class without needing secret-bearing logs, and the failure does not masquerade as healthy routing.

### STG-52 Rollback/kill switch

Exercise or dry-run the documented kill switch for one connection and confirm the rollback target is compatible with the candidate's database schema/state.

Pass condition: routing can be stopped quickly without deleting persistent history and rollback does not require bypassing `proxy_required` or discarding newer routing/idempotency state.

## Acceptance rule

The staging result is `PASS` only when every mandatory `STG-*` check above is `PASS` on the same exact candidate SHA.

Any `FAIL` or `NOT_RUN` keeps production readiness unproven. A later successful rerun must record the new evidence explicitly; do not rewrite earlier failed evidence as if it never happened.

Passing this runbook still does not authorize a merge to `main`. After successful staging and the real backup drill, the human owner must compare the exact `dev` candidate with the current working `main` deployment and explicitly approve that exact SHA before any production-baseline change.