# Simplified single-client architecture

This branch intentionally abandons the multi-tenant control-plane design for the current product stage.

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

## Admin

The admin is exposed by the `integration` service at `/admin` and uses the deployment-level `INTEGRATION_ADMIN_PASSWORD`. The browser session is signed, HttpOnly, SameSite=Strict and Secure by default. State-changing forms require a session CSRF token.

The admin configures:

- Chatwoot base URL
- Chatwoot account ID
- Chatwoot inbox ID
- Chatwoot API token
- optional per-instance Meta proxy URL

The Chatwoot API token and proxy URL are persisted in the private `integration-data-v1` Docker volume. They are never rendered back into the admin page; proxy credentials are redacted in status text.

## Matrix -> Chatwoot

The integration service logs into Synapse as the provisioned Matrix admin account and consumes `/sync`. On the first sync it only stores the checkpoint, preventing historical messages from being imported automatically.

For subsequent text events from bridge portal rooms, the sidecar creates one Chatwoot contact/conversation per Matrix room and stores the mapping locally. Messages are then created as incoming Chatwoot messages. The Matrix admin user and the mautrix appservice bot are excluded to avoid obvious echo/management traffic.

Current scope is text messages. Attachments, reactions, edits and encrypted Matrix rooms are intentionally deferred.

## Chatwoot -> Matrix

Chatwoot posts `message_created` webhooks to `/webhooks/chatwoot/<secret>`. Outgoing non-private agent messages are routed to the Matrix room mapped to that Chatwoot conversation. mautrix-meta then delivers the Matrix message to Meta.

The webhook path secret comes from `CHATWOOT_WEBHOOK_SECRET`. Message IDs are persisted for duplicate suppression. An event is marked processed only after the downstream delivery succeeds.

## Proxy

mautrix-meta v26.07 supports `network.get_proxy_from`. The bridge is configured to call the integration sidecar at `/internal/proxy` when it needs a proxy. This allows the admin to change the per-instance proxy without maintaining a mautrix-meta fork.

When proxying is disabled, the endpoint returns an empty proxy URL and mautrix-meta uses direct connectivity. When enabled, the configured HTTP/HTTPS/SOCKS proxy is returned. Media, E2EE transport, Messenger Lite and other Meta traffic classes are configured to use the same proxy hook.

## Deliberate limitations

This branch is not the multi-tenant platform. It does not provide tenant isolation, shared-instance account routing, dynamic per-account proxy pools, operator RBAC, PostgreSQL, distributed queues or cross-customer orchestration.

For the current product hypothesis those features are considered premature. The operational unit is the whole stack: one client, one Matrix account, one Chatwoot configuration and at most one proxy configuration.
