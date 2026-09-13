# Production Coolify checklist

This checklist applies to the simplified single-client stack only. One deployment serves one customer.

## Public routing

Expose only these services through Coolify/Traefik:

- `synapse:8008` on the Matrix domain.
- `integration:8080` on a separate HTTPS admin/integration domain.

Do not expose `mautrix-meta:29319` publicly.

The integration domain serves `/admin` and the Chatwoot webhook. The Meta proxy resolver remains on the same HTTP service but requires internal HTTP Basic authentication; unauthenticated requests return 404. mautrix-meta reaches it through the private Compose network.

## Required Coolify secrets

Set strong, unrelated values for:

- `MATRIX_ADMIN_PASSWORD`
- `INTEGRATION_ADMIN_PASSWORD`
- `INTEGRATION_SESSION_SECRET`
- `CHATWOOT_WEBHOOK_SECRET`
- `META_PROXY_RESOLVER_SECRET`

Use a URL-safe value for `META_PROXY_RESOLVER_SECRET`, preferably a long hexadecimal token such as the output of `openssl rand -hex 32`. Keep `INTEGRATION_COOKIE_SECURE=true` and `ALLOW_INSECURE_CHATWOOT=false` in production.

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

The resolver secret is not the residential proxy password and must not be reused for any other purpose. The transitional secret-in-path resolver is disabled. Gunicorn access logs are disabled so the Chatwoot webhook path secret is not written to normal application access logs.

## Chatwoot setup

In `/admin`, configure:

1. Chatwoot HTTPS base URL.
2. Account ID.
3. Inbox ID.
4. User API access token with access to that account/inbox.
5. Run **Test Chatwoot** and require success.

Configure a Chatwoot `message_created` webhook to:

```text
https://<integration-domain>/webhooks/chatwoot/<CHATWOOT_WEBHOOK_SECRET>
```

Treat the webhook URL as sensitive because the path contains the deployment secret.

## Meta / Matrix setup

1. Confirm Synapse is healthy.
2. Log in to Matrix with `MATRIX_ADMIN_MXID`.
3. Start a management room with the mautrix-meta bot.
4. Complete the Facebook/Messenger login.
5. Confirm new Meta portal rooms are unencrypted on Matrix. This is required because the Chatwoot sidecar intentionally does not implement Matrix E2EE.

Meta's own Messenger E2EE is separate and remains supported by mautrix-meta; its network traffic is configured to use the same Meta proxy hook.

## Mandatory live acceptance before production traffic

Repository CI cannot substitute for these tests because the residential proxy only accepts the real VPS and CI does not have your real Meta or Chatwoot accounts.

Require all of the following on the exact deployed revision:

- `/admin` loads through HTTPS and login succeeds.
- Chatwoot connection test succeeds for the configured inbox.
- Proxy egress test succeeds and the observed IP is not the VPS IP.
- Facebook/Messenger login succeeds while the proxy is enabled.
- A new inbound Meta text message reaches the correct Chatwoot conversation once.
- An agent reply in Chatwoot reaches the correct Meta conversation once.
- An ordinary non-bridge Matrix room is not forwarded to Chatwoot.
- Restart `integration`; admin configuration remains present.
- Restart/redeploy the stack without deleting volumes; Meta login state, Matrix state and Chatwoot room mappings remain present.
- Temporarily make the residential proxy unreachable; Meta traffic must fail rather than silently succeed through the VPS public IP.
- Restore the proxy and confirm reconnect/message flow recovers.

Do not declare the deployment production-ready until every item above passes on the VPS.

## Backups

The named volumes contain credentials and state and must be backed up before real customer traffic:

- `synapse-data-v2`
- `mautrix-meta-data-v2`
- `integration-data-v1`

Backups must be stored off the VPS, access-controlled and restorable. A backup is not valid until a restore into clean volumes has been tested.

## Upgrade policy

Keep image versions pinned. Test mautrix-meta/Synapse upgrades on a disposable copy or separate deployment first. Never replace the production image with `latest` as an upgrade strategy.
