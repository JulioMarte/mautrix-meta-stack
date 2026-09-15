# mautrix-meta stack for Coolify

Minimal deployment of **Synapse + mautrix-meta** for testing Facebook Messenger / Marketplace bridging on Coolify.

This repository consumes official prebuilt images and is designed so that a normal Coolify deployment bootstraps the required Synapse and mautrix files automatically. No SSH bootstrap is required for a fresh installation.

> Product direction: Matrix, Synapse, mautrix management rooms, and Element are infrastructure. The intended customer-facing interface is the integration panel. See [ADR-001: Managed Meta onboarding without Element](docs/ADR-001-managed-meta-onboarding.md).

## Architecture

```text
Internet
   |
   v
Coolify / Traefik
   |
   +--> Synapse :8008
            |
            | private Compose network
            v
       mautrix-meta :29319
            |
            v
        Meta / Facebook
```

Only Synapse should receive a public domain in the current PoC. The mautrix-meta appservice remains private.

The future product backend will call the mautrix provisioning API server-to-server over the private network. The browser must not receive the provisioning shared secret and must not call the private appservice port directly.

## Pinned images

- Synapse: `matrixdotorg/synapse:v1.160.0`
- mautrix-meta: `dock.mau.dev/mautrix/meta:v26.07`
- yq bootstrap helper: `mikefarah/yq:4.47.2`

Do not casually switch mautrix-meta to `latest`: upstream documents that `latest` follows the latest commit, not necessarily the latest stable release.

### Known version gap

As of **2026-09-15**, upstream has released mautrix-meta `v26.08.1`. That release includes a fix for a breaking change on Messenger servers. This repository is intentionally not upgraded as part of the documentation change, but the current `v26.07` pin must be reviewed and tested before implementing the managed onboarding layer.

Keep explicit release pins. Do not replace the pin with `latest`.

## Coolify setup

Create a Git-based application using the **Docker Compose** build pack.

- Branch: `main`
- Base directory: `/`
- Docker Compose location: `/compose.yaml`

Set these variables in Coolify:

```env
MATRIX_SERVER_NAME=matrix.example.com
MATRIX_ADMIN_MXID=@admin:matrix.example.com
MATRIX_ADMIN_PASSWORD=replace-with-a-strong-password
TZ=America/Santo_Domingo
SYNAPSE_IMAGE=matrixdotorg/synapse:v1.160.0
MAUTRIX_META_IMAGE=dock.mau.dev/mautrix/meta:v26.07
YQ_IMAGE=mikefarah/yq:4.47.2
```

`MATRIX_ADMIN_PASSWORD` is a secret. Store it as a secret in Coolify; do not commit a real value.

The stack uses Docker named volumes rather than host bind paths:

```text
synapse-data-v2
mautrix-meta-data-v2
```

This is deliberate for Coolify. It avoids host-path interpolation restrictions and avoids first-deploy ownership problems caused by root-owned bind-mount directories. The same named volumes are reused by the init jobs and the long-running services, so generated identity, databases and authentication state survive normal redeployments.

## Automatic bootstrap

A fresh `docker compose up` executes an idempotent init chain before the long-running services start:

```text
synapse-init
      |
      +-------------------+
      |                   |
      v                   v
mautrix-config-init   Synapse config generated
      |
      v
mautrix-configure
      |
      v
mautrix-registration-init
      |
      v
synapse-configure
      |
      v
synapse-check-config
      |
      v
Synapse
      |
      | healthy
      v
synapse-admin-init
      |
      v
mautrix-meta
```

The init services do the following:

1. Generate `homeserver.yaml` and Synapse signing identity on first deployment.
2. Generate mautrix-meta `config.yaml` if it does not already exist.
3. Configure `network.mode=facebook` and `network.marketplace_space=true`.
4. Configure mautrix-meta SQLite for the PoC.
5. Configure internal addresses `http://synapse:8008` and `http://mautrix-meta:29319`.
6. Restrict bridge login permission to the configured Matrix domain and grant the configured MXID bridge-admin permission.
7. Generate `registration.yaml` if it does not already exist.
8. Mount that registration into Synapse and configure `app_service_config_files` automatically.
9. Validate the Synapse configuration before startup.
10. Start Synapse only after the init/config validation jobs succeed.
11. Ensure the configured Matrix admin account exists using `MATRIX_ADMIN_PASSWORD`.
12. Start mautrix-meta only after Synapse is healthy and the admin provisioner succeeds.

Existing generated config, signing identity, SQLite state and appservice tokens are preserved across normal redeployments. Init jobs are expected to exit with code 0 after completing their work; they are not long-running services.

## Domain

Assign a domain only to the `synapse` service and route it to internal port `8008`, for example:

```text
https://matrix.example.com:8008
```

Do not assign a public domain to `mautrix-meta` and do not publish `29319` directly.

Synapse readiness can be verified publicly at:

```text
https://matrix.example.com/_matrix/client/versions
```

The bridge readiness endpoint is intentionally internal:

```text
http://mautrix-meta:29319/_matrix/mau/ready
```

## Matrix admin account

The configured Matrix admin account is created automatically by the one-shot `synapse-admin-init` service after Synapse becomes healthy.

The localpart comes from `MATRIX_ADMIN_MXID`; the password comes from `MATRIX_ADMIN_PASSWORD`. The provisioner uses `register_new_matrix_user --exists-ok`, so a normal redeploy does not require recreating the account manually.

For the PoC, keep public registration disabled.

The current single Matrix admin identity is a PoC convenience, not the final multi-tenant identity model. Tenant isolation must be designed before production use. See ADR-001.

## Facebook authentication: current PoC vs target product

### Current PoC

For transport validation, a Matrix client and the bridge management flow may still be used to establish the Meta session manually.

The validation target remains:

```text
Facebook login
    -> existing Messenger / Marketplace chats sync
    -> new inbound message reaches Matrix
    -> Matrix reply reaches Facebook
    -> redeploy/restart
    -> bridge reconnects using persisted state
    -> missed Marketplace messages backfill
```

### Target product flow

Element and bridge bot commands are not the intended customer onboarding path.

The target is:

```text
Integration panel
    -> Connect Facebook
    -> backend starts mautrix provisioning login flow
    -> UI/helper completes the step types requested by mautrix
    -> provisioning reports complete
    -> integration shows Connected
    -> conversations sync without the customer opening Element
```

mautrix bridgev2 models login as a state machine. Depending on the selected flow it may require `user_input`, `cookies`, `client_http`, `webauthn`, or `display_and_wait` steps.

A normal browser page cannot reliably extract cross-origin Meta cookies. Therefore **do not build the final authentication path as a Facebook iframe or ordinary popup and do not ask customers to copy cookies from developer tools**. Cookie/webview steps may require a trusted local helper such as an Electron application, following the same technical model used by upstream `mautrix-manager`.

See [ADR-001](docs/ADR-001-managed-meta-onboarding.md) for the full architecture, security boundaries, implementation phases, and acceptance criteria.

Do not add Chatwoot as a product dependency until the underlying transport loop is reliable. Once transport reliability is proven, Chatwoot integration and automatic Matrix membership handling become part of the productization phases described in ADR-001.

## Persistence and security

Runtime state is persisted in the two Docker named volumes. Conceptually they contain:

```text
synapse-data-v2
├── homeserver.yaml
├── *.signing.key
├── homeserver.db
└── media_store/

mautrix-meta-data-v2
├── config.yaml
├── registration.yaml
└── mautrix-meta.db
```

Treat both volumes as sensitive. In particular, `registration.yaml`, signing keys, SQLite databases, bridge credentials, Meta session material, and Matrix access tokens must never be committed.

Do not delete or recreate the named volumes during a normal redeploy. Removing them intentionally resets the corresponding service identity and data.

For the future onboarding API, the mautrix provisioning shared secret must remain server-side. Raw Meta cookies, passwords, tokens, WebAuthn assertions, and Matrix access tokens must not be written to application logs.

## Operational constraints

- Run exactly one long-running mautrix-meta instance against a given data volume.
- Keep Synapse and mautrix-meta in the same Compose application so they can use service-name DNS.
- Keep the appservice/provisioning port private.
- SQLite is intentional for this one-account PoC, not for the eventual multi-tenant product.
- Before production/multi-tenant use, migrate Synapse and mautrix-meta to appropriate PostgreSQL-backed deployments and establish backups.
- Define explicit tenant-to-Matrix/mautrix identity isolation before onboarding multiple independent customers.
- Meta bridging is unofficial and upstream protocol changes can temporarily break connectivity; this PoC must prove reliability before product assumptions are made.
- Test the provisioning API against the exact pinned mautrix-meta image; do not assume the moving OpenAPI schema on upstream `main` is identical to every release.

## Upstream references

- mautrix-meta documentation: https://docs.mau.fi/bridges/go/meta/
- Meta authentication: https://docs.mau.fi/bridges/go/meta/authentication.html
- v26.07 configuration reference: https://docs.mau.fi/configs/mautrix-meta/v26.07.html
- bridgev2 provisioning API schema: https://github.com/mautrix/go/blob/main/bridgev2/matrix/provisioning.yaml
- mautrix-manager: https://github.com/mautrix/manager
- mautrix-meta releases: https://github.com/mautrix/meta/releases

## Git/Coolify deployment policy

`main` is the Coolify deployment branch. Changes should be prepared and validated on a non-deployment branch, then merged once. This avoids triggering a series of partial deployments while infrastructure changes are still being assembled.
