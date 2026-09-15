# Development Branch Workflow

Status: normative repository workflow

## Branch roles

`dev` is the primary integration branch for all ongoing development. Feature, fix, documentation and experiment branches merge into `dev`, not directly into `main`.

`main` is deployment-triggering state and represents the currently human-accepted working deployment baseline. It MUST NOT be changed merely because `dev` is green, a phase is complete, or an automated agent considers a candidate deployable.

The normal development flow is:

```text
feature/*, fix/*, docs/*
          |
          v
         dev
```

Promotion from `dev` to `main` is a separate human-controlled release action, not the automatic next step in the development flow.

Direct feature-to-`main` merges are forbidden. Emergency production changes also require explicit human authorization and must be reconciled back into `dev` immediately.

## Mandatory human promotion gate

No agent, CI workflow, bot, automation, or unattended process may merge, fast-forward, reset, or otherwise move `main` to a newer integration state.

A `main` promotion is permitted only after all of the following are true:

1. the exact `dev` candidate has the required deterministic CI evidence;
2. all known unproven/manual properties are listed explicitly;
3. controlled staging evidence required by the deployment contract has been reviewed;
4. rollback/backup implications are understood;
5. a human owner has evaluated the candidate against the currently working `main` deployment; and
6. that human gives an explicit instruction to promote the identified candidate SHA to `main`.

Silence, a general instruction to continue development, a request to finish the docs, or a green CI result is never authorization to change `main`.

Until that explicit approval exists, `main` is treated as immutable from the development workflow.

## Current branch transition

`dev` was created from the validated tip of `feature/meta-control-plane` at commit `fcad2d6e7a0e4ad9933cdaaee0432e1c275f27be`. This preserves the complete Phase 0 architecture/contracts and the repository guardrails already established there.

`feature/meta-control-plane` is no longer the integration target. It remains historical context and may be retired after its work is represented in `dev`.

## Phase branch policy

Each implementation phase SHOULD use a focused branch from the current `dev` tip, for example:

```text
feature/control-plane-phase-1
feature/egress-resolver-phase-2
feature/mautrix-egress-phase-3
feature/matrix-chatwoot-phase-4
feature/chatwoot-matrix-phase-5
feature/multitenant-proof-phase-6
```

A phase branch may merge into `dev` only when the corresponding gate in `meta-control-plane-ci-acceptance.md` passes on its candidate commit. If the gate is incomplete, the branch may still be reviewed, but the capability must not be described as complete.

## Merge discipline

1. Branch from the latest green `dev`.
2. Keep the branch scoped to one phase or one coherent fix.
3. Update tests and contracts in the same branch when behavior changes.
4. Require CI on the exact head commit.
5. Merge to `dev` only after the phase gate is satisfied.
6. Treat promotion from `dev` to `main` as a distinct human-controlled release decision.
7. Never use `main` as a development base when `dev` contains newer integration work.
8. Never infer permission to change `main` from prior merge permissions on `dev`.

## Upstream mautrix handling

The repository remains a GitHub fork of `mautrix/meta`, but the stack branch history is no longer an upstream-shaped code tree. For Phase 3, the immutable recovery branch `upstream/mautrix-meta-v0.2607.0` points to upstream tag `v0.2607.0`, commit `ed37c9e6ce47e83dc75b9abea7b636302715b9bc`.

That branch is a reference baseline, not an integration branch. The mautrix fork delta must remain mechanically comparable to it.

## CI policy

CI runs on pull requests and on pushes to `dev`, `main`, and active long-lived integration branches where useful. Phase-specific jobs should be added as implementation appears rather than hiding all assertions inside one broad smoke test.

`main` being green does not prove an unmerged phase branch. `dev` being green does not waive a phase-specific acceptance requirement. The exact candidate SHA remains the unit of evidence.

CI may establish technical evidence. CI does not authorize production promotion.

## Honest status reporting

Use these terms precisely:

- **implemented**: code exists and has relevant automated tests;
- **phase complete**: every required gate for that phase is green on the exact candidate commit;
- **integrated**: merged into `dev` and `dev` is green;
- **technically deployable candidate**: `dev` satisfies the currently claimed automated deployment contract, with all manual/staging gaps explicitly listed;
- **human-approved release candidate**: the human owner has reviewed the technically deployable candidate and explicitly approved the identified SHA for release evaluation/promotion;
- **production-ready**: only after the final multi-tenant, failure-path, persistence, egress-isolation and real-environment proofs required by the branch plan and deployment contract are satisfied;
- **promoted**: `main` was changed only after explicit human authorization for the exact candidate.

Do not collapse these states into one another.
