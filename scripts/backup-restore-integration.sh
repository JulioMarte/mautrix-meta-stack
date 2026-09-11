#!/usr/bin/env bash
set -euo pipefail

: "${CONTROL_PLANE_ADMIN_TOKEN:?CONTROL_PLANE_ADMIN_TOKEN is required}"
: "${CONTROL_PLANE_INTERNAL_TOKEN:?CONTROL_PLANE_INTERNAL_TOKEN is required}"
: "${CHATWOOT_CI_TOKEN:?CHATWOOT_CI_TOKEN is required}"
: "${MATRIX_CLIENT_CI_TOKEN:?MATRIX_CLIENT_CI_TOKEN is required}"
: "${EGRESS_PROXY_BACKUP_PASSWORD:?EGRESS_PROXY_BACKUP_PASSWORD is required}"

export CONTROL_PLANE_BACKUP_DIR="${CONTROL_PLANE_BACKUP_DIR:-$PWD/.ci-backup}"
rm -rf "$CONTROL_PLANE_BACKUP_DIR"
mkdir -p "$CONTROL_PLANE_BACKUP_DIR"

DC=(docker compose -f compose.yaml -f compose.control-plane.yaml -f compose.backup-restore.yaml)

cleanup() {
  "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$CONTROL_PLANE_BACKUP_DIR"
}
trap cleanup EXIT

wait_healthy() {
  local service="$1" end=$((SECONDS + ${2:-90})) cid status
  cid="$(${DC[@]} ps -q "$service")"
  test -n "$cid"
  while ((SECONDS < end)); do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid")"
    [[ "$status" == healthy ]] && return 0
    [[ "$status" == unhealthy || "$status" == exited || "$status" == dead ]] && return 1
    sleep 2
  done
  return 1
}

run_bun() {
  "${DC[@]}" exec -T control-plane bun -
}

"${DC[@]}" config -q
"${DC[@]}" up -d chatwoot-backup-double matrix-backup-double control-plane
wait_healthy chatwoot-backup-double 60
wait_healthy matrix-backup-double 60
wait_healthy control-plane 90

run_bun <<'BUN'
const base = "http://127.0.0.1:3000";
const admin = { authorization: `Bearer ${process.env.CONTROL_PLANE_ADMIN_TOKEN}`, "content-type": "application/json" };
const internal = { authorization: `Bearer ${process.env.CONTROL_PLANE_INTERNAL_TOKEN}`, "content-type": "application/json" };
async function post(path, body, headers = admin) {
  const response = await fetch(base + path, { method: "POST", headers, body: JSON.stringify(body) });
  const text = await response.text();
  if (!response.ok) throw new Error(`${path}: ${response.status} ${text}`);
  return text ? JSON.parse(text) : {};
}
const tenant = (await post("/api/v1/tenants", { slug: "backup-restore", name: "Backup Restore" })).data;
const profile = (await post("/api/v1/egress-profiles", {
  provider: "fixture", scheme: "http", host: "proxy-backup.example", port: 8080,
  username: "backup-ci", secretRef: "env:EGRESS_PROXY_BACKUP_PASSWORD", status: "healthy",
})).data;
const connection = (await post("/api/v1/meta-connections", {
  tenantId: tenant.id, matrixOwnerMxid: "@backup:matrix.example.com",
  metaAccountId: "meta-backup-1", mautrixLoginId: "login-backup-1",
})).data;
const binding = (await post("/api/v1/chatwoot-bindings", {
  tenantId: tenant.id, chatwootAccountId: "91", chatwootInboxId: "92",
  apiBaseUrl: "http://chatwoot-backup-double:8080", credentialRef: "env:CHATWOOT_CI_TOKEN", status: "active",
})).data;
await post(`/api/v1/meta-connections/${connection.id}/egress`, { egressProfileId: profile.id });
await post(`/api/v1/meta-connections/${connection.id}/chatwoot`, { chatwootBindingId: binding.id });
await post(`/api/v1/meta-connections/${connection.id}/activate`, {});
const inbound = {
  connectionId: connection.id, roomId: "!backup:matrix.example.com", remoteThreadId: "backup-thread",
  remoteContactId: "backup-contact", eventId: "$backup-event-1", senderId: "backup-contact",
  text: "backup state seed", occurredAt: "2026-09-11T15:00:00.000Z", provenance: "meta",
};
const delivered = await post("/internal/v1/matrix/events", inbound, internal);
if (delivered.data?.status !== "delivered") throw new Error("seed Matrix event not delivered");
const { Database } = await import("bun:sqlite");
const db = new Database("/data/control-plane.db");
const conv = db.query("select id,chatwoot_conversation_id from conversation_bindings where meta_connection_id=?").get(connection.id);
if (!conv) throw new Error("conversation binding was not created");
const counts = {
  tenants: db.query("select count(*) n from tenants where id=?").get(tenant.id).n,
  connections: db.query("select count(*) n from meta_connections where id=?").get(connection.id).n,
  profiles: db.query("select count(*) n from egress_profiles where id=?").get(profile.id).n,
  chatwoot: db.query("select count(*) n from chatwoot_bindings where id=?").get(binding.id).n,
  conversations: db.query("select count(*) n from conversation_bindings where id=?").get(conv.id).n,
  processed: db.query("select count(*) n from processed_events where source='matrix' and source_event_id=?").get("$backup-event-1").n,
  audits: db.query("select count(*) n from audit_events where entity_id=?").get(connection.id).n,
};
for (const [name, count] of Object.entries(counts)) if (Number(count) < 1) throw new Error(`seed ${name} missing`);
await Bun.write("/data/backup-acceptance.json", JSON.stringify({
  tenantId: tenant.id, profileId: profile.id, connectionId: connection.id, bindingId: binding.id,
  conversationId: conv.id, chatwootConversationId: conv.chatwoot_conversation_id,
}));
db.close();
BUN

# Cold backup: stop the SQLite owner, then archive the entire persistent directory
# so a residual WAL/journal is preserved together with the main database.
"${DC[@]}" stop control-plane >/dev/null
"${DC[@]}" run --rm control-plane-backup-helper 'set -eu; test -s /data/control-plane.db; tar -C /data -czf /backup/control-plane-data.tgz .; test -s /backup/control-plane-data.tgz'
test -s "$CONTROL_PLANE_BACKUP_DIR/control-plane-data.tgz"

# Destroy the source volume and restore into a newly-created named volume.
"${DC[@]}" down -v --remove-orphans >/dev/null
"${DC[@]}" run --rm control-plane-backup-helper 'set -eu; tar -C /data -xzf /backup/control-plane-data.tgz; test -s /data/control-plane.db'
"${DC[@]}" up -d chatwoot-backup-double matrix-backup-double control-plane
wait_healthy chatwoot-backup-double 60
wait_healthy matrix-backup-double 60
wait_healthy control-plane 90

run_bun <<'BUN'
const { Database } = await import("bun:sqlite");
const expected = JSON.parse(await Bun.file("/data/backup-acceptance.json").text());
const db = new Database("/data/control-plane.db", { readonly: true });
const connection = db.query("select * from meta_connections where id=?").get(expected.connectionId);
if (!connection) throw new Error("restored connection missing");
if (connection.meta_account_id !== "meta-backup-1" || connection.mautrix_login_id !== "login-backup-1") throw new Error("restored identity changed");
if (connection.egress_profile_id !== expected.profileId || connection.chatwoot_binding_id !== expected.bindingId || connection.status !== "active") throw new Error("restored assignment/binding changed");
const conversation = db.query("select * from conversation_bindings where id=?").get(expected.conversationId);
if (!conversation || conversation.chatwoot_conversation_id !== expected.chatwootConversationId || conversation.matrix_room_id !== "!backup:matrix.example.com") throw new Error("restored conversation route changed");
const processed = db.query("select status,payload_hash,meta_connection_id from processed_events where source='matrix' and source_event_id=?").get("$backup-event-1");
if (!processed || processed.status !== "delivered" || processed.meta_connection_id !== expected.connectionId || !processed.payload_hash) throw new Error("restored processed event missing");
const audits = db.query("select count(*) n from audit_events where entity_id=?").get(expected.connectionId);
if (Number(audits.n) < 1) throw new Error("restored audit history missing");
const tenant = db.query("select slug,status from tenants where id=?").get(expected.tenantId);
if (!tenant || tenant.slug !== "backup-restore" || tenant.status !== "active") throw new Error("restored tenant missing");
db.close();
const event = {
  connectionId: expected.connectionId, roomId: "!backup:matrix.example.com", remoteThreadId: "backup-thread",
  remoteContactId: "backup-contact", eventId: "$backup-event-1", senderId: "backup-contact",
  text: "backup state seed", occurredAt: "2026-09-11T15:00:00.000Z", provenance: "meta",
};
const response = await fetch("http://127.0.0.1:3000/internal/v1/matrix/events", {
  method: "POST",
  headers: { authorization: `Bearer ${process.env.CONTROL_PLANE_INTERNAL_TOKEN}`, "content-type": "application/json" },
  body: JSON.stringify(event),
});
const body = await response.json();
if (!response.ok || body.data?.status !== "duplicate") throw new Error(`restored event was not duplicate: ${response.status} ${JSON.stringify(body)}`);
const chatwoot = await (await fetch("http://chatwoot-backup-double:8080/_test/state")).json();
if (chatwoot.messages.length !== 0) throw new Error("restored duplicate caused a new Chatwoot side effect");
BUN

# Acceptance metadata is deliberately non-secret; runtime credentials stay in
# environment/Coolify rather than the restored state contract.
archive_metadata="$(tar -xOzf "$CONTROL_PLANE_BACKUP_DIR/control-plane-data.tgz" ./backup-acceptance.json 2>/dev/null || true)"
for secret in "$CONTROL_PLANE_ADMIN_TOKEN" "$CONTROL_PLANE_INTERNAL_TOKEN" "$CHATWOOT_CI_TOKEN" "$MATRIX_CLIENT_CI_TOKEN" "$EGRESS_PROXY_BACKUP_PASSWORD"; do
  printf '%s' "$archive_metadata" | grep -Fq "$secret" && { echo "runtime secret leaked into backup acceptance metadata" >&2; exit 1; }
done

echo "Control-plane backup/restore acceptance passed"
