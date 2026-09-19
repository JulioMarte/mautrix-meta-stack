# mautrix-meta stack for Coolify

Minimal single-client deployment of **Synapse + mautrix-meta + a NiceGUI Chatwoot integration/admin sidecar** for Facebook Messenger / Marketplace bridging on Coolify.

The stack consumes official prebuilt Synapse and mautrix-meta images and bootstraps their required files automatically. The repository-owned `integration` container provides Matrix <-> Chatwoot text transport, the visual NiceGUI admin, and the authenticated Meta proxy resolver.

## Simplified product model

One deployed stack serves one customer. If another customer needs the service, deploy another stack. The preserved multi-tenant/control-plane work remains on `archive/multi-tenant-control-plane-2026-09-12` and is intentionally not part of this deployment line.

## Architecture

```text
Internet
   |
   v
Coolify / Traefik
   |
   +--> Synapse :8008
   |
   +--> integration :8080
           |   NiceGUI /admin
           |   Chatwoot webhook
           |   authenticated proxy resolver
           |
           +--> Chatwoot API
           +--> Synapse client API

private Compose network
   |
   +--> mautrix-meta :29319
           |
           +--> Meta / Facebook through optional residential proxy
```

Only Synapse and the `integration` service need public domains. `mautrix-meta` remains private.

## Coolify

Create a Git-based Docker Compose application using `/compose.yaml`. Production configuration and acceptance steps are documented in `docs/production-coolify-checklist.md`.

Important runtime variables include:

```env
MATRIX_SERVER_NAME=matrix.example.com
MATRIX_ADMIN_MXID=@admin:matrix.example.com
MATRIX_ADMIN_PASSWORD=<secret>
INTEGRATION_ADMIN_PASSWORD=<secret>
INTEGRATION_SESSION_SECRET=<secret>
CHATWOOT_WEBHOOK_SECRET=<secret>
META_PROXY_RESOLVER_SECRET=<url-safe-secret>
INTEGRATION_COOKIE_SECURE=true
ALLOW_INSECURE_CHATWOOT=false
NICEGUI_STORAGE_PATH=/data/nicegui
```

For a residential Meta proxy, use dedicated variables rather than process-wide `HTTP_PROXY`/`HTTPS_PROXY`:

```env
META_PROXY_ENABLED=true
META_PROXY_URL=http://proxy-user:proxy-password@proxy-host:8888
```

The actual Chatwoot base URL, account ID, inbox ID and API token are configured visually at `/admin`. When `META_PROXY_URL` is supplied by Coolify, the admin shows it only in redacted/read-only form.

## Admin

The operator UI is implemented with **NiceGUI 3.16.0** and runs on the integration service at `/admin`. It provides:

- authenticated login;
- Chatwoot configuration;
- proxy status/configuration when not managed by Coolify;
- Chatwoot connectivity testing;
- residential proxy egress testing;
- secret redaction;
- persisted state through the `integration-data-v1` volume.

NiceGUI uses a WebSocket after the initial page load, so the integration domain must allow WebSocket upgrades through Coolify/Traefik.

## Managed Meta onboarding

The production Facebook Messenger onboarding path is `/admin/meta`. The panel
queries the login flows exposed by the pinned mautrix-meta BridgeV2 provisioning
API and prioritizes `messenger-lite-android`, which can be completed as
`user_input` steps inside the authenticated admin UI.

Normal users do not need Element, Matrix bot commands, developer tools, direct
access to the mautrix provisioning port, or manual cookie extraction. The
provisioning shared secret remains server-side.

`/admin/meta-cookie` remains available only as a recovery fallback for web-cookie
flows. Raw cookies, passwords, OTP values and provisioning tokens are not written
to normal logs. Login failures emit credential-safe correlation references and
stable failure codes so operators can trace one attempt in container logs.

`/health` is a lightweight process liveness endpoint. `/ready` is the product
readiness diagnostic and reports local configuration plus Synapse, mautrix
provisioning and Meta-session readiness without exposing credentials. Use
`/ready?deep=1` during deployment acceptance to include a live Chatwoot check.

The architecture, security boundary and acceptance criteria are documented in
`docs/architecture/managed-meta-onboarding.md`.

## Persistence

Docker named volumes are used deliberately:

```text
synapse-data-v2
mautrix-meta-data-v2
integration-data-v1
```

The integration volume contains its SQLite database and NiceGUI server-side user storage. Do not delete these volumes during a normal redeploy.

## Security boundaries

- The integration container has a read-only root filesystem, `no-new-privileges`, and all Linux capabilities dropped.
- NiceGUI storage is explicitly written to `/data/nicegui` because `/app` is read-only.
- New Chatwoot webhooks use the canonical `/webhooks/chatwoot` endpoint with timestamped HMAC verification. The secret-in-path endpoint is migration-only.
- The Meta proxy resolver requires internal HTTP Basic authentication and returns 404 when unauthenticated.
- Residential proxy credentials belong in `META_PROXY_URL` in Coolify, not in Git.
- Matrix-side portal encryption is disabled because the Chatwoot sidecar does not implement Matrix crypto. This does not disable Meta/Messenger E2EE handled by mautrix-meta.
- Initial mautrix thread backfill is disabled and a Chatwoot activation boundary prevents old Matrix events from flooding Chatwoot.

## Acceptance boundary

Repository CI validates build, Compose bootstrap, NiceGUI startup, health endpoints, configuration persistence across restart, proxy-resolver authentication and the mautrix policy. It cannot prove the VPS-restricted residential proxy, real Meta authentication, or a real Meta <-> Matrix <-> Chatwoot round trip.

Do not treat a green repository build as production acceptance. Run `docs/production-coolify-checklist.md` on the exact deployed revision before sending real customer traffic.
