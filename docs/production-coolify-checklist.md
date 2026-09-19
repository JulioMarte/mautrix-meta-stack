# Production Coolify checklist

This checklist applies to the simplified single-client stack only. One deployment serves one customer.

## Public routing

Expose only these services through Coolify/Traefik:

- `synapse:8008` on the Matrix domain.
- `integration:8080` on a separate HTTPS admin/integration domain.

Do not expose `mautrix-meta:29319` publicly.

The integration domain serves `/admin` and the Chatwoot webhook. The admin is a NiceGUI application and requires normal WebSocket upgrade support through Traefik; Coolify/Traefik normally handles this automatically, but it must be verified after deployment. The Meta proxy resolver remains on the same HTTP service but requires internal HTTP Basic authentication; unauthenticated requests return 404. mautrix-meta reaches it through the private Compose network.

## Required Coolify secrets

Set strong, unrelated values for:

- `MATRIX_ADMIN_PASSWORD`
- `INTEGRATION_ADMIN_PASSWORD`
- `INTEGRATION_SESSION_SECRET`
- `CHATWOOT_WEBHOOK_SECRET`
- `META_PROXY_RESOLVER_SECRET`

Use a URL-safe value for `META_PROXY_RESOLVER_SECRET`, preferably a long hexadecimal token such as the output of `openssl rand -hex 32`. Keep `INTEGRATION_COOKIE_SECURE=true` and `ALLOW_INSECURE_CHATWOOT=false` in production.

NiceGUI server-side user storage is explicitly placed at `/data/nicegui`, inside `integration-data-v1`. Do not move it back to the read-only container filesystem.

## Residential proxy

Use dedicated Meta proxy variables rather than global process proxy variables:

```env
META_PROXY_ENABLED=true
META_PROXY_URL=http://proxy-user:proxy-password@proxy-host:8888
```

Store `META_PROXY_URL` as a secret in Coolify. Do not commit the real value and do not define global `HTTP_PROXY`/`HTTPS_PROXY` for the stack.

For the current residential provider, the HTTP endpoint is preferred because it has already been verified from the Contabo VPS. Docker bridge egress is normally NATed through the VPS, so a provider restricted to that VPS source should see the expected source host.

After deployment, use **Test proxy egress** in `/admin`. The observed IP must be a residential/provider exit IP and must not be the Contabo/VPS public IP.

## Internal resolver

mautrix-meta is configured with `network.get_proxy_from` pointing to `/internal/proxy` on the integration container. The URL carries HTTP Basic credentials internally: username `mautrix`, password `META_PROXY_RESOLVER_SECRET`.

The resolver secret is not the residential proxy password and must not be reused for any other purpose. The transitional secret-in-path resolver is disabled. NiceGUI/Uvicorn access logging is disabled so the Chatwoot webhook path secret is not written to normal application access logs.

## Chatwoot setup

The production outbound path is a **Chatwoot API Inbox callback**, not an
account-level webhook.

In `/admin/basic`:

1. Configure the Chatwoot HTTPS base URL and Personal Access Token.
2. Detect the account and select a numeric inbox whose `channel_type` is `Channel::Api`.
3. Save and run the Chatwoot API test.
4. Apply and verify the API Inbox callback. The required callback URL is:

```text
https://<integration-domain>/webhooks/chatwoot/inbox
```

5. The panel verifies that Chatwoot stored that exact callback and imports the
   API Inbox HMAC token into the private integration volume.
6. Send a real agent reply and require a verified callback delivery plus a
   positively acknowledged Matrix event in **Status & tests**.

The callback rejects stale timestamps and invalid `X-Chatwoot-Signature` HMACs.
New deployments should not create an account-level Settings → Integrations →
Webhook for this connector. The old `/webhooks/chatwoot` path remains only for
migration; after the API Inbox callback and a real reply test pass, remove legacy
account-level hooks to avoid two outbound delivery paths.

## Meta onboarding

1. Confirm `/health` returns 200 and use `/ready` for product readiness diagnostics.
2. Open `/admin/meta`.
3. Choose **Messenger Android — recomendado**.
4. Complete every Meta credential, checkpoint, OTP/CAPTCHA or follow-up step shown by the managed BridgeV2 flow.
5. Require the admin to show an actual connected Meta login before continuing.
6. Use `/admin/meta-cookie` only as a recovery fallback if the recommended upstream flow is unavailable.
7. Confirm new Meta portal rooms are unencrypted on Matrix. This is required because the Chatwoot sidecar intentionally does not implement Matrix E2EE.

Normal onboarding must not require Element, Matrix management rooms or bridge bot commands. Element remains an internal diagnostic tool only. Meta's own Messenger E2EE is separate and remains supported by mautrix-meta; its network traffic is configured to use the same Meta proxy hook.

## Mandatory live acceptance before production traffic

Repository CI cannot substitute for these tests because the residential proxy only accepts the real VPS and CI does not have your real Meta or Chatwoot accounts.

Require all of the following on the exact deployed revision:

- `/admin` loads through HTTPS, the NiceGUI WebSocket remains connected and admin login succeeds.
- `/ready` reports DB, Chatwoot configuration, proxy configuration, Synapse, mautrix provisioning and Meta connection state without exposing secrets.
- The recommended `messenger-lite-android` login succeeds from `/admin/meta` without Element or browser developer tools.
- Saving Chatwoot configuration from the NiceGUI panel survives an `integration` restart.
- Chatwoot connection test succeeds for the configured inbox.
- Proxy egress test succeeds and the observed IP is not the VPS IP.
- Facebook/Messenger login succeeds while the proxy is enabled.
- A new inbound Meta text message reaches the correct Chatwoot conversation once.
- An agent reply in Chatwoot reaches the correct Meta conversation once.
- An ordinary non-bridge Matrix room is not forwarded to Chatwoot.
- Restart `integration`, then restart `mautrix-meta`, then perform a normal full redeploy without deleting volumes; after each operation Meta login state, Matrix state, NiceGUI user storage and Chatwoot room mappings remain present and message flow recovers.
- Temporarily make the residential proxy unreachable; Meta traffic must fail rather than silently succeed through the VPS public IP.
- Restore the proxy and confirm reconnect/message flow recovers.

Do not declare the deployment production-ready until every item above passes on the VPS.

## Backups

The named volumes contain credentials and state and must be backed up before real customer traffic:

- `synapse-data-v2`
- `mautrix-meta-data-v2`
- `integration-data-v1`

Backups must be stored off the VPS, access-controlled and restorable. A backup is not valid until a restore has been tested.

The repository includes cold-backup helpers for the current single-client stack:

```bash
BACKUP_ROOT=/secure/backups bash scripts/backup-current-stack.sh
```

The backup stops the three state owners while archiving their named volumes, writes SHA-256 checksums and restarts the stack. The resulting directory contains secrets/state and must be encrypted or otherwise strongly access-controlled when moved off the VPS.

Restore is deliberately destructive and requires an explicit confirmation value:

```bash
BACKUP_DIR=/secure/backups/<timestamp> \
CONFIRM_RESTORE=RESTORE \
bash scripts/restore-current-stack.sh
```

After restore, require `/health`, `/ready`, Meta login state and the full bidirectional message acceptance checks before reopening production traffic.

## Upgrade policy

Keep image versions pinned. NiceGUI is pinned to `3.16.0` in the integration image. Test NiceGUI, mautrix-meta and Synapse upgrades on a disposable copy or separate deployment first. Never replace production dependencies with unpinned `latest` versions as an upgrade strategy.


## Repository release gate

Before promoting `dev` to `main`, GitHub branch protection or a repository ruleset must require successful status checks. At minimum require:

- `compose`
- `runtime-composition`
- `binding-generations`
- `onboarding`
- `live-provisioning-contract`
- `admin-v2`
- `tests`

Direct pushes that bypass these checks must be disabled for the production branch. The repository currently documents this requirement because branch/ruleset administration is outside the runtime code path; verify it in GitHub settings before production release.
