# Production Readiness Ledger

Status: authoritative current-state ledger for the current single-client mautrix-meta ↔ Matrix/Synapse ↔ Chatwoot stack.

This file is the release truth source. Historical control-plane and multi-tenant plans remain useful design history, but they are not release criteria for the production runtime in `compose.yaml`.

## Promotion rule

`dev` is the integration branch. `main` is the production baseline.

No release is production-ready merely because a feature branch or PR is green. Promotion to `main` requires:

1. one exact `dev` candidate SHA;
2. all required repository checks green for that SHA;
3. the controlled staging runbook passing against that exact SHA;
4. a tested off-host backup/restore path;
5. an explicit human decision to promote that exact candidate.

Evidence from different SHAs must not be combined.

## Current runtime architecture

```text
Meta / Messenger / Marketplace
          ↕
       mautrix-meta
          ↕
      Matrix / Synapse
          ↕
 Python/NiceGUI integration
          ↕
   Chatwoot API Inbox
```

The production integration is the Python runtime under `integration/`. The legacy/multi-tenant TypeScript control-plane is not production-authoritative.

Current pinned infrastructure includes:

- Synapse `matrixdotorg/synapse:v1.160.0`;
- mautrix-meta runtime based on `v26.08.1` plus repository-controlled patches;
- Chatwoot contract coverage against v4.7.0 behavior;
- persistent Synapse, mautrix-meta and integration volumes.

## Repository-proven behavior

The current repository test matrix proves, with local services/test doubles where appropriate:

- generation-scoped Chatwoot binding identity;
- durable external Meta identity independent from ephemeral Chatwoot conversation IDs;
- stale Chatwoot projection recovery without reusing old-generation conversations;
- intentional conversation deletion tombstones and bidirectional deletion lifecycle;
- generation-scoped event deduplication;
- Matrix portal auto-join and bounded history import without outbound replay;
- contact display-name/avatar reconciliation;
- live reconciliation after profile/history policy changes;
- Matrix ↔ Chatwoot delivery acknowledgement and duplicate suppression;
- Chatwoot API Inbox callback HMAC verification and authenticated fallback for documented HMAC mismatch behavior;
- destructive deletion requiring a verified API-Inbox signature;
- Meta onboarding through the BridgeV2 provisioning API;
- `messenger-lite-android` as the supported primary browser-safe `user_input` flow;
- cookie/helper onboarding only as a recovery fallback;
- secret-safe login diagnostics with one correlation reference across a login attempt;
- production runtime composition using the same import/install order as the container;
- SQLite connection closure for integration and mautrix metadata readers;
- process liveness separated from product readiness;
- cold backup/restore helpers for the three current authoritative volumes.

CI includes real Synapse + integration + mautrix-meta containers for repository E2E journeys. Chatwoot and Meta provider behavior are partially represented by controlled doubles/contracts; CI does not contain real Facebook credentials.

## Required repository release checks

For the exact release candidate SHA, require successful current-head results for at least:

- `compose`;
- `runtime-composition`;
- `binding-generations`;
- `onboarding`;
- `live-provisioning-contract`;
- `admin-v2`;
- `tests`.

Additional workflows such as marketplace/thread discovery must also be green when triggered by the candidate diff.

Automatic workflows use a per-PR/ref concurrency group with `cancel-in-progress: true` so obsolete commits do not consume runner capacity or masquerade as current evidence.

## Operational interfaces

### Liveness

`GET /health` is intentionally lightweight. Container orchestration may use it without restarting the integration because an external dependency is temporarily unavailable.

### Readiness

`GET /ready` verifies the local DB/configuration plus the current Synapse, mautrix provisioning, Meta-session and Chatwoot API-Inbox delivery prerequisites.

`GET /ready?deep=1` additionally performs a live Chatwoot API-Inbox check and verifies that the selected Channel::Api still points at the canonical callback path.

Readiness output must not expose API tokens, HMAC secrets, provisioning secrets, Meta credentials or login IDs.

### Chatwoot outbound path

The supported agent-reply path is:

```text
Chatwoot Channel::Api
  -> /webhooks/chatwoot/inbox
  -> timestamp/HMAC or authenticated REST fallback
  -> mapped Matrix room
  -> Matrix event acknowledgement
  -> Meta
```

A delivery is considered verified only after Matrix returns an event ID and that event is verified in the target room.

Account-level `/webhooks/chatwoot` handling is migration compatibility, not the production happy path.

### Meta onboarding

The supported happy path is `/admin/meta` using `messenger-lite-android`.

`/admin/meta-cookie` is a recovery fallback only.

Normal onboarding must not require Element, Matrix bot commands, browser developer tools or direct access to the mautrix provisioning port.

## Production blockers that CI cannot close

### 1. Exact current-head CI completion

The release candidate cannot be merged/promoted on evidence from an earlier head. All required checks must complete on the exact candidate.

### 2. Real Meta/provider acceptance

Repository CI cannot prove future Meta checkpoints, account restrictions or provider-side changes.

Staging must prove on the exact candidate:

- a real Messenger Android login through the admin panel;
- connected state after restart/redeploy;
- a new real Messenger inbound conversation reaching Chatwoot with correct identity;
- a Chatwoot reply reaching the correct Meta thread exactly once;
- Marketplace behavior when Marketplace is part of the intended deployment;
- image/file/audio behavior used in production;
- no dependence on manually accepting rooms in Element.

### 3. Real Chatwoot acceptance

Staging must prove against the target Chatwoot instance:

- selected inbox is `Channel::Api`;
- callback URL is exactly `/webhooks/chatwoot/inbox`;
- HMAC token is imported/verified where exposed;
- a real agent reply produces a positively verified Matrix event;
- old account-level connector webhooks are removed after migration acceptance.

### 4. Persistence and recovery

The repository includes cold-backup and destructive-restore helpers, but production readiness additionally requires:

- backup stored outside the VPS/source-volume failure domain;
- appropriate encryption/access control;
- a real restore drill;
- post-restore message-path verification.

### 5. Repository governance

Branch protection/rulesets must require the release checks and prevent direct production-branch pushes. This is a GitHub administrative control and is not enforced by application code.

## Non-blocking debt

The following items are material but are not release blockers for the current single-client scope unless the production environment specifically requires them:

- broader operator RBAC beyond the single protected admin surface;
- PostgreSQL migration from SQLite;
- full OpenTelemetry/metrics stack;
- automatic release promotion;
- generalized multi-tenant identity/egress allocation;
- Instagram-specific identity transitions.

These items must not be described as already supported.

## Completion sequence

1. Finish the exact current PR head CI and audit the full compose log for hidden warnings/errors.
2. Merge the fully green hardening PR into `dev`.
3. Verify the post-merge `dev` workflows.
4. Record that exact green `dev` SHA as the staging candidate.
5. Execute `staging-acceptance-runbook.md` against the deployed candidate.
6. Perform the off-host backup and destructive restore drill.
7. Enable/verify GitHub branch protection or a ruleset requiring the release checks.
8. Review the candidate against the existing production baseline.
9. Promote to `main` only after explicit human approval of that exact SHA.

## Current readiness statement

The codebase has repository-level coverage for the critical single-client identity, delivery, deletion, onboarding, reconciliation and recovery mechanisms.

Production readiness is not claimed until the exact candidate passes current-head CI plus the real staging and backup/restore gates above.
