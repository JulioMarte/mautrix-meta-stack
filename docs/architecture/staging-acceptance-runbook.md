# Controlled Staging Acceptance Runbook

Status: normative human-evidence checklist for the current single-client production stack.

This runbook covers what repository CI cannot prove honestly: the real Meta provider, the target Chatwoot instance, Coolify/TLS/ingress behavior, persistence across real redeploys and off-host recovery.

Passing this runbook does not by itself promote `dev` to `main`.

## Entry conditions

Before staging:

- record the exact green `dev` candidate SHA;
- confirm all required current-head repository checks are green;
- ensure no uncommitted/out-of-band runtime changes exist;
- have one controlled Meta/Facebook account suitable for real Messenger testing and, when relevant, Marketplace;
- have a second Facebook account/persona capable of sending test inbound messages;
- configure a dedicated Chatwoot `Channel::Api` inbox for the candidate;
- have an off-host backup destination;
- know the current rollback target;
- ensure secrets exist only in Coolify/environment/private runtime state.

Record:

```text
candidate_sha=<exact dev SHA>
started_at=<UTC timestamp>
operator=<human operator>
environment=<staging identifier>
chatwoot_instance=<non-secret identifier>
```

If the candidate SHA changes, stop and start a new evidence record.

## Required topology

```text
public HTTPS
   |
   +-> integration admin + Chatwoot API Inbox callback

private application network
   |
   +-> integration:8080
   +-> synapse:8008
   +-> mautrix-meta:29319

persistent state
   |
   +-> integration-data-v1
   +-> synapse-data-v2
   +-> mautrix-meta-data-v2
   +-> isolated provisioning-secret volume
```

Do not publish Synapse internal port 8008, mautrix appservice/provisioning port 29319, or the internal proxy resolver directly to the Internet.

## Evidence format

For every check record:

```text
check_id
candidate_sha
started_at
finished_at
result = PASS | FAIL | NOT_RUN
observable evidence
log/artifact reference with secrets removed
operator notes
```

Never copy Meta passwords, OTPs, cookies, Chatwoot tokens, HMAC tokens, Matrix passwords or provisioning secrets into the evidence.

## Stage A — deployment and security boundary

### STG-01 Candidate identity

Deploy the exact candidate through the normal Coolify/repository path.

Pass when the running deployment can be tied to the exact recorded SHA and no manual SSH code/config patch was required.

### STG-02 Public exposure

From an external network verify:

- the admin is reachable only through intended HTTPS;
- direct container ports are not publicly reachable;
- mautrix provisioning/appservice endpoints are not public;
- the internal proxy resolver is not public;
- HTTP is redirected/rejected according to the deployment policy.

Pass when only intended HTTPS surfaces are reachable.

### STG-03 Admin security

Verify:

- `INTEGRATION_COOKIE_SECURE=true`;
- invalid admin passwords are rejected;
- ingress rate limiting is enabled for the admin/login origin;
- successful logout removes the authenticated admin state;
- browser responses do not expose stored tokens/secrets.

### STG-04 Liveness versus readiness

Verify:

- `/health` returns healthy while the integration process is functional;
- `/ready` clearly reports missing product prerequisites instead of crashing;
- after full acceptance `/ready` returns ready;
- `/ready?deep=1` succeeds and reveals no secrets.

## Stage B — Chatwoot API Inbox

### STG-10 API Inbox identity

In `/admin/basic`, discover/select the real Chatwoot inbox.

Pass when the selected inbox is numeric, is `Channel::Api`, and the API test passes.

### STG-11 Callback configuration

Apply/verify the callback from the admin.

Expected URL:

```text
https://<integration-domain>/webhooks/chatwoot/inbox
```

Pass when Chatwoot reports the exact URL and the integration has a valid API-Inbox signing secret/HMAC token when that Chatwoot version exposes it.

### STG-12 Legacy webhook cleanup

Inspect account-level Chatwoot webhooks.

Pass when no obsolete connector account webhook remains after the API-Inbox callback has been accepted. A temporary legacy hook is allowed only during migration testing and must be removed before production sign-off.

## Stage C — Meta onboarding

### STG-20 Primary Messenger Android login

Open `/admin/meta` and select **Messenger Android — recomendado**.

Complete real provider steps shown by the BridgeV2 `user_input` flow.

Pass when:

- no Element/bot-command/devtools workflow is needed;
- credentials/OTP/CAPTCHA are not printed in logs;
- the panel reaches a connected Meta session;
- any failure is represented by a stable code and correlation reference.

### STG-21 Login recovery

Restart `integration` while Meta is connected.

Pass when the persisted Meta session remains available through mautrix and the admin returns to a coherent connected state.

Restart `mautrix-meta` and repeat.

Pass when the integration detects/reports any transient unavailable state and recovers without corrupting bindings.

### STG-22 Cookie fallback isolation

Do not use the cookie flow as the normal test.

Verify only that `/admin/meta-cookie` is clearly labeled fallback/recovery and that the primary navigation still points to `/admin/meta`.

## Stage D — real message acceptance

### STG-30 New Messenger inbound

From the second Facebook identity, send a brand-new Messenger message to the connected account.

Do not open Element.

Pass when:

- the portal is automatically joined;
- one Chatwoot conversation appears;
- the real contact name appears;
- avatar synchronizes when provider/Matrix supplies one;
- the customer message appears once;
- no empty conversation projection is created.

### STG-31 Chatwoot reply to Meta

Reply from Chatwoot.

Pass when:

- the API Inbox callback is authenticated;
- the integration maps the Chatwoot conversation to the correct active binding generation;
- Matrix returns an event ID;
- the status page records the positive Matrix acknowledgement;
- the reply reaches the real Messenger thread exactly once.

A Chatwoot UI “sent” state without a verified Matrix event is a failure.

### STG-32 Duplicate callback

Replay the same safe test callback/message only through a controlled mechanism that does not send a second customer-visible message.

Pass when the message ID is deduplicated and no duplicate Matrix/Meta side effect occurs.

### STG-33 Media

Exercise the media types required by production (at minimum one image; include file/audio when those are used operationally).

Pass when inbound and outbound behavior matches documented support without duplicate delivery or secret-bearing URLs/logs.

### STG-34 Marketplace

If Marketplace is in production scope, create a new Marketplace conversation.

Pass when it materializes in the configured Chatwoot inbox with the intended context and agent reply returns to the correct Meta thread.

If Marketplace is not in production scope, record `NOT_APPLICABLE` and do not claim Marketplace support.

## Stage E — lifecycle and identity

### STG-40 Chatwoot deletion then new Meta message

Delete the controlled Chatwoot conversation through the supported deletion path.

Pass when intentional-deletion state is recorded and the deleted projection is not silently reused.

Then send a new Meta message according to the documented recreation policy.

Pass when the resulting projection belongs to the active binding generation and old-generation event dedupe does not suppress the new inbound message.

### STG-41 Binding target change

In staging only, switch to a different disposable Chatwoot API Inbox, then switch back if needed.

Pass when:

- a new binding generation is created;
- old projection IDs are never treated as authoritative for the new generation;
- webhook/callback verification state is invalidated for the changed target;
- new messages do not reuse stale conversation IDs.

### STG-42 Restart persistence

With known conversations present:

1. restart `integration`;
2. repeat one inbound/outbound test;
3. restart `mautrix-meta`;
4. repeat;
5. perform a normal full redeploy without deleting volumes;
6. repeat.

Pass when bindings, dedupe state, login state and message flow survive with no user-visible replay.

## Stage F — backup and restore

### STG-50 Off-host backup

Run:

```bash
BACKUP_ROOT=<off-host-mounted-or-secure-staging-path> \
  bash scripts/backup-current-stack.sh
```

Copy/store the completed backup outside the VPS/source-volume failure domain.

Record non-secret evidence of:

- storage location class;
- encryption/access control;
- retention policy;
- checksum file;
- manifest/candidate SHA.

### STG-51 Destructive restore drill

On staging, restore with:

```bash
BACKUP_DIR=<selected-backup-directory> \
CONFIRM_RESTORE=RESTORE \
  bash scripts/restore-current-stack.sh
```

Pass when checksums validate and the three authoritative volumes are restored without ad-hoc DB repair.

### STG-52 Post-restore acceptance

After restore require:

- `/health` healthy;
- Meta session state coherent;
- `/ready` healthy after product prerequisites are exercised;
- an existing conversation still mapped correctly;
- one new inbound message delivered exactly once;
- one Chatwoot reply produces a verified Matrix event and reaches Meta.

## Stage G — failure visibility and rollback

### STG-60 Provider/login failure

Induce a safe login failure or use a controlled invalid test input.

Pass when the panel shows a stable failure code + correlation ref and logs contain enough non-secret context to trace it.

### STG-61 Chatwoot failure

Temporarily make the staging Chatwoot target unavailable or invalid.

Pass when delivery fails visibly, no successful-delivery marker is written, and no stale target is silently used.

### STG-62 Matrix/mautrix failure

Temporarily stop the relevant staging service.

Pass when:

- `/health` continues to represent integration process liveness;
- `/ready` becomes not-ready;
- agent delivery cannot be falsely acknowledged;
- recovery does not duplicate the message.

### STG-63 Rollback

Confirm the previous production candidate and its state compatibility are documented.

Pass when rollback can be initiated without deleting current persistent volumes blindly and there is an explicit decision on whether state must be restored from the pre-release backup.

## Stage H — release governance

### STG-70 GitHub protection

Verify GitHub branch protection/ruleset settings for the production branch.

Pass when direct production pushes are blocked and required checks include at least:

- `compose`;
- `runtime-composition`;
- `binding-generations`;
- `onboarding`;
- `live-provisioning-contract`;
- `admin-v2`;
- `tests`.

### STG-71 Exact candidate review

Confirm all PASS evidence belongs to the same candidate SHA and no subsequent commit exists in the release candidate.

## Acceptance rule

The staging result is `PASS` only when all checks applicable to the production scope are `PASS` on one exact candidate SHA.

Any required `FAIL` or `NOT_RUN` blocks production readiness.

After staging PASS, the human owner must explicitly approve promotion of that exact `dev` SHA to `main`.
