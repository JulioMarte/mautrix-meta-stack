# Simplified single-client architecture

This branch intentionally replaces the current multi-tenant control-plane direction with a simpler deployment model for the current product stage.

## Product model

One deployed stack serves one customer. If a second customer needs the service, deploy a second stack. Shared multi-tenant routing, per-account egress allocation, tenant RBAC and cross-tenant recovery are deliberately out of scope.

The preserved multi-tenant work remains available on `archive/multi-tenant-control-plane-2026-09-12` and must not be deleted as part of this simplified line of development.

## Runtime components

- Synapse: local Matrix homeserver.
- mautrix-meta: upstream v26.07 Facebook/Messenger bridge.
- integration: small sidecar owned by this repository.

The integration sidecar has two responsibilities only:

1. Matrix <-> Chatwoot message transport.
2. A visual admin for Chatwoot and optional Meta proxy configuration.

The integration HTTP service runs under Gunicorn with one worker and multiple threads. One worker is intentional because the process owns exactly one Matrix `/sync` loop for this single-client deployment.

## Admin

The admin is exposed by the `integration` service at `/admin` and uses the deployment-level `INTEGRATION_ADMIN_PASSWORD`. The browser session is signed, HttpOnly, SameSite=Strict and Secure by default. State-changing forms require a session CSRF token and the login form is CSRF-protected as well.

The admin configures:

- Chatwoot base URL
- Chatwoot account ID
- Chatwoot inbox ID
- Chatwoot API token
- optional per-instance Meta proxy when the proxy is not managed by Coolify

The Chatwoot API token is persisted in the private `integration-data-v1` Docker volume and is never rendered back into the admin page. If `META_PROXY_URL` is defined in Coolify, the proxy becomes deployment-managed: the admin only shows a redacted status and cannot replace the secret value. If no deployment proxy is defined, the admin may persist a proxy in the private integration volume.

The panel also provides explicit connectivity tests for the configured Chatwoot inbox and the active proxy egress.

## Matrix -> Chatwoot

The integration service logs into Synapse as the provisioned Matrix admin account and consumes `/sync`. It only forwards rooms that contain Matrix bridge state (`m.bridge` or `uk.half-shot.bridge`), so ordinary Matrix rooms and management rooms are not treated as Chatwoot conversations.

When Chatwoot is first configured, the sidecar stores a persistent activation timestamp. Events whose Matrix `origin_server_ts` predates that boundary are not imported into Chatwoot. This prevents initial bridge history from becoming a burst of old customer messages. mautrix-meta thread backfill is also disabled for this single-client Chatwoot deployment.

For eligible live text events, the sidecar creates one Chatwoot contact/conversation per Matrix room and stores the mapping locally. It uses the `source_id` returned by Chatwoot for the configured contact inbox. If Chatwoot did not create the contact-inbox association, the sidecar creates one and uses the confirmed `source_id` from that association.

Current scope is text messages. Attachments, reactions and edits are intentionally deferred.

## Matrix encryption

Matrix-side end-to-bridge encryption is explicitly disabled (`encryption.allow/default/require=false`). The integration sidecar does not implement Matrix crypto and therefore must never depend on encrypted Matrix portal events.

This is separate from Meta's own Messenger E2EE transport. mautrix-meta may still handle Meta E2EE, and the configured Meta proxy hook is enabled for its E2EE network traffic.

## Chatwoot -> Matrix

Chatwoot posts `message_created` webhooks to `/webhooks/chatwoot/<secret>`. Outgoing non-private agent messages are routed to the Matrix room mapped to that Chatwoot conversation. mautrix-meta then delivers the Matrix message to Meta.

The webhook path secret comes from `CHATWOOT_WEBHOOK_SECRET`. Message IDs are persisted for duplicate suppression. An event is marked processed only after downstream Matrix delivery succeeds.

## Proxy

mautrix-meta v26.07 supports `network.get_proxy_from`. The bridge is configured to call a protected integration endpoint:

`/internal/proxy/<META_PROXY_RESOLVER_SECRET>`

The legacy unauthenticated `/internal/proxy` endpoint is forced to return 404. `META_PROXY_RESOLVER_SECRET` is a separate Coolify secret and must not be reused as the proxy password or webhook secret.

For production, prefer deployment-managed proxy configuration:

- `META_PROXY_ENABLED=true`
- `META_PROXY_URL=http://user:password@host:port`

Do not set global `HTTP_PROXY` or `HTTPS_PROXY` variables for this purpose. The residential proxy is intended only for Meta traffic, not Matrix or Chatwoot API calls.

When proxying is disabled, the resolver returns an empty proxy URL and mautrix-meta uses direct connectivity. When enabled, the configured HTTP/HTTPS/SOCKS proxy is returned. Media, Meta E2EE, Messenger Lite and other supported Meta traffic classes use the same proxy hook.

## Container hardening

The integration container runs with a read-only root filesystem, `no-new-privileges`, all Linux capabilities dropped and a writable private `/data` volume. A tmpfs is mounted at `/tmp` for runtime temporary files.

## Deliberate limitations

This branch is not the multi-tenant platform. It does not provide tenant isolation, shared-instance account routing, dynamic per-account proxy pools, operator RBAC, PostgreSQL, distributed queues or cross-customer orchestration.

For the current product hypothesis those features are considered premature. The operational unit is the whole stack: one client, one Matrix account, one Chatwoot configuration and at most one Meta proxy configuration.
