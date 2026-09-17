# Facebook login provisioning hardening — 2026-09-17

Status: ready for merge to `dev`; real-provider acceptance still required before promotion to `main`
Related contract: `managed-meta-onboarding.md`
Related implementation status: `managed-meta-onboarding-implementation.md`
PR: #77

## Purpose

This note records the reconciliation and hardening work completed on `fix/facebook-login-provisioning` before merging it back into `dev`. It exists because the managed Meta onboarding documents describe the architectural direction, while this branch adds concrete behavior that must remain part of the product contract and regression coverage.

The changes do not replace the existing Matrix ↔ Chatwoot runtime, conversation lifecycle, Marketplace handling, or the cookie-first production path. They harden the experimental managed BridgeV2 login path and simplify the operator-facing Basic setup UI.

## Managed login behavior

The managed login page at `/admin/meta` discovers the login methods exposed by the pinned mautrix-meta runtime. The UI behavior is now:

1. No login method is preselected when the page loads.
2. Recommended methods are ordered first, with Messenger Android preferred when available.
3. Selecting a method starts that BridgeV2 login flow immediately; there is no separate `Start connection` button.
4. Once a login process is active, another method cannot be started until the current process is completed or cancelled.
5. Login steps continue to be rendered using product-facing language and without exposing provisioning secrets or raw authentication material.

This avoids both redundant UI interaction and accidental creation of parallel login processes.

## Recovery from lost BridgeV2 login processes

BridgeV2 login process IDs are temporary runtime state. The integration persists only sanitized step metadata so the page can survive a refresh, but mautrix-meta may lose its in-memory login process after a restart or recreation.

When a persisted step references a login process that mautrix-meta no longer knows, BridgeV2 returns a 404 with `Login not found`. The integration now treats only that specific condition as a stale process:

- the persisted onboarding step is cleared;
- the operator receives a message explaining that a new connection must be started;
- the dead form is no longer left on screen;
- the behavior applies to `user_input` and `display_and_wait` continuation steps;
- unrelated 401, 404 and 500 errors are not reclassified as a stale login process.

This is implemented by `integration/meta_login_recovery.py` and covered by `integration/test_meta_login_recovery.py`.

## Basic setup callback UI

The canonical outbound callback remains the Chatwoot API Inbox callback:

```text
Chatwoot agent reply
-> integration callback
-> Matrix
-> Meta
```

The Basic setup UI no longer presents migration-era account-webhook instructions to the operator. The following visible legacy material is removed from the built admin UI:

- the sentence instructing operators that they no longer need Settings → Integrations → Webhooks;
- the `Legacy account webhook migration` card;
- the `No legacy account-level webhook ... detected` success message.

The callback URL remains readonly and now has a dedicated copy icon so the operator can copy it without selecting the text manually. `Apply & verify API Inbox callback` remains unchanged because it is part of the active configuration path.

For compatibility with existing internal regression coverage, the visual cleanup is currently applied during the integration image build by `integration/apply_basic_setup_cleanup.py`. The transformation is intentionally fail-closed: if the expected source block no longer matches, the image build fails instead of silently shipping a partially transformed admin UI. This build-time transformation is technical debt and should eventually be folded directly into `admin_v2.py` after the legacy helper compatibility requirement is retired.

## Security and runtime invariants

These changes preserve the existing invariants:

- `mautrix-meta:29319` remains private to the Compose network;
- the provisioning shared secret never reaches browser JavaScript;
- raw passwords, cookies, tokens and assertions are not persisted in generic integration settings;
- the managed login path is initiated only from the authenticated admin surface;
- existing Matrix auto-join, Chatwoot synchronization, delivery verification and conversation lifecycle behavior remain authoritative;
- no code path claims that a real Facebook account is connected merely because a login process was started.

## Reconciliation with `dev`

Before this documentation update, the branch head was `d7dbfb385636bf2b89709a38a1fcc88d4e2fc032` and GitHub reported:

- branch ahead of `dev`: 9 commits;
- branch behind `dev`: 0 commits;
- PR #77 mergeable;
- no open review threads.

Therefore there was no independent `dev` work to rebase or manually reconcile into this branch at that point.

## Automated validation completed on the reconciled code

The reconciled code head completed all of the following GitHub Actions workflows successfully:

- `Validate stack`;
- `Validate admin v2`;
- `Validate marketplace rebuild v4`;
- `Meta onboarding`;
- `Meta onboarding live stack`.

`Validate stack` covered Docker Compose validation, image build, legacy and hardening tests, runtime enhancements, conversation lifecycle and adversarial lifecycle tests, Chatwoot deletion contract, delivery history, media context, NiceGUI tests, Meta runtime routes, Synapse bootstrap preflight, real service startup and health, admin endpoints, persisted configuration across restart, mautrix policy/proxy behavior and the bidirectional conversation-deletion journey.

`Meta onboarding live stack` started the exact stack with `dock.mau.dev/mautrix/meta:v26.08.1` and exercised the real BridgeV2 provisioning state machine, provisioning-secret isolation, production route exposure and private-port behavior.

The onboarding workflow also explicitly runs the regression tests for stale login recovery and automatic method selection.

## What this merge does not prove

A green repository CI run cannot prove Facebook's external authentication behavior. The following still require real-provider staging:

- successful login with a real Facebook account using the managed Android/iOS flow;
- behavior through provider-side 2FA, checkpoint, CAPTCHA or passkey variations;
- Messenger and Marketplace round trips using the resulting real session;
- session survival through an actual Coolify redeploy;
- provider-side changes that may affect the unofficial Meta protocol.

These limitations are not blockers for merging the hardening work into `dev`; they remain blockers for declaring the managed path production-authoritative or promoting it to `main` solely on CI evidence.

## Merge rule

PR #77 may be merged into `dev` after the documentation commit has been checked for branch divergence and the required CI/check state remains acceptable. Promotion from `dev` to `main` remains a separate decision and must follow the repository's development-branch workflow and real-provider acceptance requirements.
