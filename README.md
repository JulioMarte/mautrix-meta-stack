# mautrix-meta stack for Coolify

Minimal deployment of **Synapse + mautrix-meta** for testing Facebook Messenger / Marketplace bridging on Coolify.

This repository consumes the official prebuilt images and is designed so that a normal Coolify deployment bootstraps the required Synapse and mautrix files automatically. No SSH bootstrap is required for a fresh installation.

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

Only Synapse should receive a public domain. The mautrix-meta appservice remains private.

## Pinned images

- Synapse: `matrixdotorg/synapse:v1.160.0`
- mautrix-meta: `dock.mau.dev/mautrix/meta:v26.07`
- yq bootstrap helper: `mikefarah/yq:4.47.2`

Do not casually switch mautrix-meta to `latest`: upstream documents that `latest` follows the latest commit, not necessarily the latest stable release.

## Coolify setup

Create a Git-based application using the **Docker Compose** build pack.

- Branch: `main`
- Base directory: `/`
- Docker Compose location: `/compose.yaml`

Set these variables in Coolify:

```env
MATRIX_SERVER_NAME=matrix.example.com
MATRIX_ADMIN_MXID=@admin:matrix.example.com
TZ=America/Santo_Domingo
SYNAPSE_IMAGE=matrixdotorg/synapse:v1.160.0
MAUTRIX_META_IMAGE=dock.mau.dev/mautrix/meta:v26.07
YQ_IMAGE=mikefarah/yq:4.47.2
```

The persistent bind-mount paths are intentionally fixed in `compose.yaml` because Coolify rejects `${...}` interpolation inside volume source paths:

```text
/data/mautrix-meta-stack/synapse
/data/mautrix-meta-stack/mautrix-meta
```

Docker creates the required host directories when the bind mounts are first used.

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
Synapse
      |
      | healthy
      v
mautrix-meta
```

The init services do the following:

1. Generate `homeserver.yaml`, signing keys and the initial Synapse SQLite database location on first deployment.
2. Generate mautrix-meta `config.yaml` if it does not already exist.
3. Configure `network.mode=facebook` and `network.marketplace_space=true`.
4. Configure mautrix-meta SQLite for the PoC.
5. Configure internal addresses `http://synapse:8008` and `http://mautrix-meta:29319`.
6. Restrict bridge login permission to the configured Matrix domain and grant the configured MXID bridge-admin permission.
7. Generate `registration.yaml` if it does not already exist.
8. Mount that registration into Synapse and configure `app_service_config_files` automatically.
9. Start Synapse only after all init jobs succeed.
10. Start mautrix-meta only after Synapse is healthy.

Existing generated config, signing identity and appservice tokens are preserved across redeployments. Init jobs are expected to exit successfully after completing their work; they are not long-running services.

## Domain

Assign a domain only to the `synapse` service and route it to internal port `8008`, for example:

```text
https://matrix.example.com:8008
```

Do not assign a domain to `mautrix-meta` and do not publish `29319` directly.

Synapse readiness can be verified publicly at:

```text
https://matrix.example.com/_matrix/client/versions
```

The bridge readiness endpoint is intentionally internal:

```text
http://mautrix-meta:29319/_matrix/mau/ready
```

## Create the first Matrix user

User credentials are deliberately **not** stored in Git or generated automatically. Once Synapse is healthy, open the Synapse container terminal in Coolify and run:

```bash
register_new_matrix_user -c /data/homeserver.yaml http://localhost:8008
```

Create the localpart matching the MXID configured by `MATRIX_ADMIN_MXID`. For the PoC, keep public registration disabled.

## Facebook authentication

After logging into the homeserver from a Matrix client, start a management room with the Meta bridge bot and use the current mautrix-meta login flow. The initial validation target is:

```text
Facebook login
    -> existing Messenger / Marketplace chats sync
    -> new inbound message reaches Matrix
    -> Matrix reply reaches Facebook
    -> redeploy/restart
    -> bridge reconnects using persisted state
    -> missed Marketplace messages backfill
```

Do not add Chatwoot until this transport loop is reliable.

## Persistence and security

Runtime state remains on the Coolify host under:

```text
/data/mautrix-meta-stack/
├── synapse/
│   ├── homeserver.yaml
│   ├── *.signing.key
│   ├── homeserver.db
│   └── media_store/
└── mautrix-meta/
    ├── config.yaml
    ├── registration.yaml
    └── mautrix-meta.db
```

Treat the entire directory as sensitive. In particular, `registration.yaml`, signing keys, SQLite databases, bridge credentials and Matrix access tokens must never be committed.

## Operational constraints

- Run exactly one long-running mautrix-meta instance against a given data directory.
- Keep Synapse and mautrix-meta in the same Compose application so they can use service-name DNS.
- Keep the appservice port private.
- SQLite is intentional for this one-account PoC, not for the eventual multi-tenant product.
- Before production/multi-tenant use, migrate Synapse and mautrix-meta to separate PostgreSQL databases and establish backups.
- Meta bridging is unofficial and upstream protocol changes can temporarily break connectivity; this PoC must prove reliability before product assumptions are made.

## Git/Coolify deployment policy

`main` is the Coolify deployment branch. Changes should be prepared and validated on a non-deployment branch, then merged once. This avoids triggering a series of partial deployments while infrastructure changes are still being assembled.
