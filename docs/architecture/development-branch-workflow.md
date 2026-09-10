# Development Branch Workflow

Status: normative repository workflow

## Branch roles

`dev` is the primary integration branch for all ongoing development. Feature, fix, documentation and experiment branches merge into `dev`, not directly into `main`.

`main` is deployment-triggering state. It receives changes only by deliberate promotion from `dev` after the relevant CI gates are green on the exact candidate commit.

The normal flow is:

```text
feature/*, fix/*, docs/*
          |
          v
         dev
          |
          v
        main
```

Direct feature-to-`main` merges are forbidden except for a narrowly scoped emergency hotfix, and any such exception must be documented and reconciled back into `dev` immediately.

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
6. Promote `dev` to `main` only at an explicit deployable checkpoint.
7. Never use `main` as a development base when `dev` contains newer integration work.

## Upstream mautrix handling

The repository remains a GitHub fork of `mautrix/meta`, but the stack branch history is no longer an upstream-shaped code tree. For Phase 3, the immutable recovery branch `upstream/mautrix-meta-v0.2607.0` points to upstream tag `v0.2607.0`, commit `ed37c9e6ce47e83dc75b9abea7b636302715b9bc`.

That branch is a reference baseline, not an integration branch. The mautrix fork delta must remain mechanically comparable to it.

## CI policy

CI runs on pull requests and on pushes to `dev`, `main`, and active long-lived integration branches where useful. Phase-specific jobs should be added as implementation appears rather than hiding all assertions inside one broad smoke test.

`main` being green does not prove an unmerged phase branch. `dev` being green does not waive a phase-specific acceptance requirement. The exact candidate SHA remains the unit of evidence.

## Honest status reporting

Use these terms precisely:

- **implemented**: code exists and has relevant automated tests;
- **phase complete**: every required gate for that phase is green on the exact candidate commit;
- **integrated**: merged into `dev` and `dev` is green;
- **deployable checkpoint**: `dev` satisfies the currently claimed deployment contract and is intentionally ready for promotion;
- **production-ready**: only after the final multi-tenant, failure-path, persistence and egress-isolation proofs required by the branch plan.

Do not collapse these states into one another.
