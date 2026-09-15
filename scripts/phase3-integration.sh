#!/usr/bin/env bash
set -euo pipefail

: "${CONTROL_PLANE_ADMIN_TOKEN:?CONTROL_PLANE_ADMIN_TOKEN is required}"
: "${CONTROL_PLANE_INTERNAL_TOKEN:?CONTROL_PLANE_INTERNAL_TOKEN is required}"
: "${EGRESS_PROXY_CI_PASSWORD:?EGRESS_PROXY_CI_PASSWORD is required}"

base_dc=(docker compose -f compose.yaml -f compose.control-plane.yaml -f compose.ci.yaml)
phase3_dc=(docker compose -f compose.yaml -f compose.control-plane.yaml -f compose.ci.yaml -f compose.phase3.yaml)

cleanup() {
  "${phase3_dc[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Generate the normal clean-state configs first. Registration/config generation is
# intentionally still exercised against the pinned release contract.
docker compose -f compose.yaml up --abort-on-container-exit --exit-code-from synapse-check-config synapse-check-config

# Configure only this integration topology for dynamic account-aware egress.
# Messenger Lite is intentionally disabled: on the pinned upstream it performs
# network I/O before a stable account identity exists, so dynamic egress cannot
# safely bind that first request yet.
docker compose -f compose.yaml run --rm --no-deps --entrypoint /bin/sh mautrix-configure -c '
  set -eu
  yq -i '\''
    .network.get_proxy_from = "http://control-plane:3000/internal/v1/egress/resolve" |
    .network.proxy_other = true |
    .network.proxy_media = true |
    .network.proxy_e2ee = true |
    .network.proxy_messenger_lite = false
  '\'' /data/config.yaml
'

"${phase3_dc[@]}" up -d control-plane synapse mautrix-meta

wait_healthy() {
  local service="$1" timeout="$2" cid status end
  cid="$("${phase3_dc[@]}" ps -q "$service")"
  test -n "$cid"
  end=$((SECONDS + timeout))
  while (( SECONDS < end )); do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid")"
    echo "$service: $status"
    [[ "$status" == healthy ]] && return 0
    [[ "$status" =~ ^(unhealthy|exited|dead)$ ]] && return 1
    sleep 3
  done
  return 1
}

wait_healthy synapse 120
wait_healthy control-plane 90
wait_healthy mautrix-meta 120

# Establish one active account assignment using only the provider identity known
# before first login. The BridgeV2 login ID is deliberately left unbound here so
# the later resolver call proves progressive identity resolution without relying
# on a pre-populated mautrix_login_id fixture.
"${phase3_dc[@]}" exec -T control-plane bun -e '
  const base="http://127.0.0.1:3000";
  const headers={authorization:`Bearer ${process.env.CONTROL_PLANE_ADMIN_TOKEN}`,"content-type":"application/json"};
  const post=async(path,body)=>{const r=await fetch(base+path,{method:"POST",headers,body:JSON.stringify(body)});if(!r.ok){console.error(path,r.status);process.exit(1)}return (await r.json()).data};
  const tenant=await post("/api/v1/tenants",{slug:"phase3-mautrix",name:"Phase 3 Mautrix"});
  const profile=await post("/api/v1/egress-profiles",{provider:"fixture",scheme:"socks5",host:"proxy-ci.test",port:1080,username:"ci",secretRef:"env:EGRESS_PROXY_CI_PASSWORD",status:"healthy"});
  const connection=await post("/api/v1/meta-connections",{tenantId:tenant.id,matrixOwnerMxid:"@phase3:matrix.example.com",metaAccountId:"123"});
  if(connection.mautrixLoginId!==null) process.exit(1);
  await post(`/api/v1/meta-connections/${connection.id}/egress`,{egressProfileId:profile.id});
  await post(`/api/v1/meta-connections/${connection.id}/activate`,{});
'

# Check the actual patched runtime container: configuration, service credential,
# network reachability and authenticated resolver behavior. The resolver receives
# both the known provider account and the newly available deterministic BridgeV2
# login ID even though only the provider identity was prebound above.
"${phase3_dc[@]}" exec -T mautrix-meta /bin/sh -c '
  set -eu
  test -n "$MAUTRIX_META_EGRESS_TOKEN"
  test "$(yq ".network.get_proxy_from" /data/config.yaml)" = "http://control-plane:3000/internal/v1/egress/resolve"
  test "$(yq ".network.proxy_other" /data/config.yaml)" = "true"
  test "$(yq ".network.proxy_media" /data/config.yaml)" = "true"
  test "$(yq ".network.proxy_e2ee" /data/config.yaml)" = "true"
  test "$(yq ".network.proxy_messenger_lite" /data/config.yaml)" = "false"
  curl -fsS -H "Authorization: Bearer $MAUTRIX_META_EGRESS_TOKEN" \
    "http://control-plane:3000/internal/v1/egress/resolve?meta_account_id=123&login_id=123&reason=connect&traffic_class=messaging" \
    -o /tmp/phase3-resolver.json
  jq -e '\''(.assignment_id | length) > 0'\'' /tmp/phase3-resolver.json >/dev/null
  jq -e '\''(.proxy_url | length) > 0'\'' /tmp/phase3-resolver.json >/dev/null
  rm -f /tmp/phase3-resolver.json
'

logs="$("${phase3_dc[@]}" logs --no-color mautrix-meta control-plane 2>&1 || true)"
if grep -Fq -- "$EGRESS_PROXY_CI_PASSWORD" <<<"$logs"; then
  echo "raw proxy credential leaked into service logs" >&2
  exit 1
fi
if grep -Fq -- "$CONTROL_PLANE_INTERNAL_TOKEN" <<<"$logs"; then
  echo "internal resolver token leaked into service logs" >&2
  exit 1
fi

echo "Phase 3 patched topology integration passed"
