# Simplified single-client architecture

This branch intentionally replaces the current multi-tenant control-plane direction with a simpler deployment model for the current product stage.

## Product model

One deployed stack serves one customer. If a second customer needs the service, deploy a second stack. Shared multi-tenant routing, per-account egress allocation, tenant RBAC and cross-tenant recovery are deliberately out of scope.

The preserved multi-tenant work remains available on `archive/multi-tenant-control-plane-2026-09-12` and must not be deleted as part of this simplified line of development.

## Runtime components

- Synapse: local Matrix homeserver.
- mautrix-meta: upstream v26.07 Facebook/Messenger bridge.
- integration: small Python sidecar owned by this repository.

The integration sidecar has two responsibilities only: Matrix <-> Chatwoot message transport and the visual operator surface. The operator surface is implemented with NiceGUI 3.16.0 on top of its FastAPI/Uvicorn runtime. The service intentionally runs as one process because this single-client deployment owns exactly one Matrix `/sync` loop.

## Admin

The admin is exposed by the `integration` service at `/admin`. The UI is built with NiceGUI rather than hand-written server-rendered HTML. NiceGUI's per-user storage is signed with `INTEGRATION_SESSION_SECRET`; the session cookie is configured `SameSite=Strict`, Secure in production and limited to eight hours.

The admin configures the Chatwoot base URL, account ID, inbox ID and API token. It can also configure a per-instance Meta proxy when the proxy is not managed by Coolify. The Chatwoot API token is persisted in the private `integration-data-v1` volume and is never rendered back. If `META_PROXY_URL` is defined in Coolify, the proxy becomes deployment-managed: the admin shows a redacted status and cannot replace the secret value.

The panel provides explicit actions for saving configuration, testing the configured Chatwoot inbox, testing the active proxy egress and logging out. NiceGUI event callbacks replace traditional state-changing HTML form posts; API endpoints remain separately authenticated.

## Matrix -> Chatwoot

The integration service logs into Synapse as the provisioned Matrix admin account and consumes `/sync`. It only forwards rooms containing Matrix bridge state (`m.bridge` or `uk.half-shot.bridge`), so ordinary Matrix rooms and management rooms are not treated as Chatwoot conversations.

When Chatwoot is first configured, the sidecar stores a persistent activation timestamp. Matrix events older than that boundary are not imported into Chatwoot, and mautrix-meta thread backfill is disabled. This prevents historical conversations becoming a burst of old customer messages.

For eligible live text events, the sidecar creates one Chatwoot contact/conversation per Matrix room and stores the mapping locally. It uses the `source_id` returned by Chatwoot for the configured contact inbox. If Chatwoot did not create the association, the sidecar creates one and uses the confirmed `source_id` from that association.

Current scope is text messages. Attachments, reactions and edits are intentionally deferred.

## Matrix encryption

Matrix-side end-to-bridge encryption is explicitly disabled (`encryption.allow/default/require=false`). The integration sidecar does not implement Matrix crypto and therefore must not depend on encrypted Matrix portal events.

This is separate from Meta's own Messenger E2EE transport. mautrix-meta may still handle Meta E2EE, and its network traffic uses the same Meta proxy hook.

## Chatwoot -> Matrix

Chatwoot posts `message_created` webhooks to `/webhooks/chatwoot/<secret>`. Outgoing non-private agent messages are routed to the Matrix room mapped to that Chatwoot conversation. mautrix-meta then delivers the Matrix message to Meta.

The webhook secret comes from `CHATWOOT_WEBHOOK_SECRET`. Message IDs are persisted for duplicate suppression. An event is marked processed only after downstream Matrix delivery succeeds. Uvicorn access logging is disabled so secret-bearing webhook paths are not written to normal application access logs.

## Proxy

mautrix-meta v26.07 supports `network.get_proxy_from`. The bridge calls `http://integration:8080/internal/proxy` over the private Compose network using HTTP Basic authentication. The fixed username is `mautrix`; the password is `META_PROXY_RESOLVER_SECRET`.

Unauthenticated or incorrectly authenticated resolver requests return 404. The previous secret-in-path resolver is disabled, so the internal resolver password does not appear in request URLs. `META_PROXY_RESOLVER_SECRET` must be a separate URL-safe Coolify secret and must not be reused as the residential proxy password or webhook secret. A long hexadecimal token is recommended.

For production, prefer deployment-managed proxy configuration:

- `META_PROXY_ENABLED=true`
- `META_PROXY_URL=http://user:password@host:port`

Do not set global `HTTP_PROXY` or `HTTPS_PROXY` variables for this purpose. The residential proxy is intended only for Meta traffic, not Matrix or Chatwoot API calls.

When proxying is disabled, the authenticated resolver returns an empty proxy URL and mautrix-meta uses direct connectivity. When enabled, it returns the configured HTTP/HTTPS/SOCKS proxy. Media, Meta E2EE, Messenger Lite and other supported Meta traffic classes use the same hook.

## Container hardening

The integration container runs with a read-only root filesystem, `no-new-privileges`, all Linux capabilities dropped and a writable private `/data` volume. A tmpfs is mounted at `/tmp` for temporary files. NiceGUI/Uvicorn runs a single process and the service is expected to remain behind Coolify/Traefik HTTPS.

## Deliberate limitations

This branch is not the multi-tenant platform. It does not provide tenant isolation, shared-instance account routing, dynamic per-account proxy pools, operator RBAC, PostgreSQL, distributed queues or cross-customer orchestration.

For the current product hypothesis those features are premature. The operational unit is the whole stack: one client, one Matrix account, one Chatwoot configuration and at most one Meta proxy configuration.
