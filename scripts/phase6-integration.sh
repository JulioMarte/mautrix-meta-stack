#!/usr/bin/env bash
set -euo pipefail

for name in \
  CONTROL_PLANE_ADMIN_TOKEN CONTROL_PLANE_INTERNAL_TOKEN CHATWOOT_CI_TOKEN \
  CHATWOOT_WEBHOOK_PHASE6_A_SECRET CHATWOOT_WEBHOOK_PHASE6_B_SECRET MATRIX_CLIENT_CI_TOKEN \
  EGRESS_PROXY_PHASE6_A_PASSWORD EGRESS_PROXY_PHASE6_B_PASSWORD; do
  test -n "${!name:-}" || { echo "$name is required" >&2; exit 1; }
done

DC=(docker compose -f compose.yaml -f compose.control-plane.yaml -f compose.phase6.yaml)
cleanup() { "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; }
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
"${DC[@]}" up -d chatwoot-double matrix-client-double proxy-a proxy-b direct-egress-sentinel control-plane
for service in chatwoot-double matrix-client-double proxy-a proxy-b direct-egress-sentinel control-plane; do
  wait_healthy "$service" 90
done

# Two tenants, two bindings, two egress profiles. Exercise all protected traffic
# classes over real proxy sockets and both message directions with interleaved IDs.
run_bun <<'BUN'
import { createHmac } from "node:crypto";
import { connect } from "node:net";

const base = "http://127.0.0.1:3000";
const admin = { authorization: `Bearer ${process.env.CONTROL_PLANE_ADMIN_TOKEN}`, "content-type": "application/json" };
const internal = { authorization: `Bearer ${process.env.CONTROL_PLANE_INTERNAL_TOKEN}`, "content-type": "application/json" };

async function req(method, path, body, headers = admin, expected) {
  const response = await fetch(base + path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  const parsed = text ? JSON.parse(text) : {};
  const ok = expected === undefined ? response.ok : response.status === expected;
  if (!ok) throw new Error(`${method} ${path}: ${response.status} ${text}`);
  return parsed;
}
const post = (path, body, headers = admin, expected) => req("POST", path, body, headers, expected);
const get = (path, headers = internal) => req("GET", path, undefined, headers);

async function makeRoute(suffix, account, inbox, proxyHost, secretRef, hookRef) {
  const tenant = (await post("/api/v1/tenants", { slug: `phase6-${suffix}`, name: `Phase 6 ${suffix.toUpperCase()}` })).data;
  const profile = (await post("/api/v1/egress-profiles", {
    provider: "fixture", scheme: "http", host: proxyHost, port: 8081,
    username: `ci-${suffix}`, secretRef, status: "healthy",
  })).data;
  if (profile.secretRef !== "[configured]") throw new Error("egress secret ref exposed");

  const connection = (await post("/api/v1/meta-connections", {
    tenantId: tenant.id,
    matrixOwnerMxid: `@phase6-${suffix}:matrix.example.com`,
    metaAccountId: `meta-phase6-${suffix}`,
    mautrixLoginId: `login-phase6-${suffix}`,
  })).data;
  const binding = (await post("/api/v1/chatwoot-bindings", {
    tenantId: tenant.id,
    chatwootAccountId: String(account), chatwootInboxId: String(inbox),
    apiBaseUrl: "http://chatwoot-double:8080",
    credentialRef: "env:CHATWOOT_CI_TOKEN", status: "active",
  })).data;
  await post(`/api/v1/meta-connections/${connection.id}/egress`, { egressProfileId: profile.id });
  await post(`/api/v1/meta-connections/${connection.id}/chatwoot`, { chatwootBindingId: binding.id });
  await post(`/api/v1/meta-connections/${connection.id}/activate`, {});
  const hook = (await post(`/api/v1/chatwoot-bindings/${binding.id}/webhook`, { secretRef: hookRef })).data;
  if (hook.secretRef !== "[configured]") throw new Error("webhook secret ref exposed");
  return { tenant, connection, binding };
}

const a = await makeRoute("a", 1, 10, "proxy-a", "env:EGRESS_PROXY_PHASE6_A_PASSWORD", "env:CHATWOOT_WEBHOOK_PHASE6_A_SECRET");
const b = await makeRoute("b", 2, 20, "proxy-b", "env:EGRESS_PROXY_PHASE6_B_PASSWORD", "env:CHATWOOT_WEBHOOK_PHASE6_B_SECRET");

function proxyRequest(proxyUrl, target) {
  return new Promise((resolve, reject) => {
    const proxy = new URL(proxyUrl);
    let data = "";
    let settled = false;
    const socket = connect(Number(proxy.port), proxy.hostname, () => {
      const auth = Buffer.from(`${decodeURIComponent(proxy.username)}:${decodeURIComponent(proxy.password)}`).toString("base64");
      socket.write(`GET ${target} HTTP/1.1\r\nHost: direct-egress-sentinel:8083\r\nProxy-Authorization: Basic ${auth}\r\nConnection: close\r\n\r\n`);
    });
    socket.setTimeout(3000, () => socket.destroy(new Error("proxy timeout")));
    socket.on("data", chunk => data += chunk.toString());
    socket.on("error", error => { if (!settled) { settled = true; reject(error); } });
    socket.on("close", () => { if (!settled) { settled = true; resolve(data); } });
  });
}

const assignments = { a: new Set(), b: new Set() };
for (const trafficClass of ["login", "messaging", "media", "e2ee"]) {
  for (const [key, route] of [["a", a], ["b", b]]) {
    const query = new URLSearchParams({
      meta_account_id: route.connection.metaAccountId,
      login_id: route.connection.mautrixLoginId,
      reason: `phase6-${trafficClass}`,
      traffic_class: trafficClass,
    });
    const resolved = await get(`/internal/v1/egress/resolve?${query}`);
    assignments[key].add(resolved.assignment_id);
    const expectedHost = key === "a" ? "proxy-a" : "proxy-b";
    if (new URL(resolved.proxy_url).hostname !== expectedHost) throw new Error(`wrong ${key} proxy`);
    const response = await proxyRequest(resolved.proxy_url, `http://direct-egress-sentinel:8083/protected/${key}/${trafficClass}`);
    if (!String(response).includes(" 200 ")) throw new Error(`proxy ${key}/${trafficClass} failed`);
  }
}
if (assignments.a.size !== 1 || assignments.b.size !== 1 || [...assignments.a][0] === [...assignments.b][0]) {
  throw new Error("assignment isolation failed");
}
const proxyA = await (await fetch("http://proxy-a:8081/_test/state")).json();
const proxyB = await (await fetch("http://proxy-b:8081/_test/state")).json();
const direct = await (await fetch("http://direct-egress-sentinel:8083/_test/state")).json();
if (proxyA.hits.length !== 4 || proxyB.hits.length !== 4 || direct.hits !== 0) throw new Error("observable egress isolation failed");

const eventA = { connectionId: a.connection.id, roomId: "!phase6-a:matrix.example.com", remoteThreadId: "thread-phase6-a", remoteContactId: "contact-phase6-a", eventId: "$phase6-a-1", senderId: "contact-phase6-a", text: "from meta A", occurredAt: "2026-09-11T14:00:00.000Z", provenance: "meta" };
const eventB = { connectionId: b.connection.id, roomId: "!phase6-b:matrix.example.com", remoteThreadId: "thread-phase6-b", remoteContactId: "contact-phase6-b", eventId: "$phase6-b-1", senderId: "contact-phase6-b", text: "from meta B", occurredAt: "2026-09-11T14:00:01.000Z", provenance: "meta" };
if ((await post("/internal/v1/matrix/events", eventA, internal)).data.status !== "delivered") throw new Error("Matrix A not delivered");
if ((await post("/internal/v1/matrix/events", eventB, internal)).data.status !== "delivered") throw new Error("Matrix B not delivered");
if ((await post("/internal/v1/matrix/events", eventA, internal)).data.status !== "duplicate") throw new Error("Matrix duplicate not suppressed");

const chatwoot = await (await fetch("http://chatwoot-double:8080/_test/state")).json();
const convA = chatwoot.conversations.find(item => item.inboxId === 10);
const convB = chatwoot.conversations.find(item => item.inboxId === 20);
if (chatwoot.messages.length !== 2 || !convA || !convB || convA.id === convB.id) throw new Error("Chatwoot A/B route split failed");

async function sign(route, secret, payload, expected = 200) {
  const raw = JSON.stringify(payload);
  const timestamp = String(Math.floor(Date.now() / 1000));
  const signature = "sha256=" + createHmac("sha256", secret).update(`${timestamp}.${raw}`).digest("hex");
  return post(`/webhooks/chatwoot/${route.binding.id}`, payload, {
    "content-type": "application/json",
    "x-chatwoot-timestamp": timestamp,
    "x-chatwoot-signature": signature,
    "x-chatwoot-delivery": `phase6-${route.binding.id}-${payload.id}`,
  }, expected);
}
const payloadA = { event: "message_created", id: 6001, message_type: "outgoing", private: false, content: "agent A", created_at: "2026-09-11T14:01:00.000Z", account: { id: 1 }, inbox: { id: 10 }, conversation: { id: convA.id }, sender: { id: 81, type: "user", name: "Agent A" }, attachments: [] };
const payloadB = { event: "message_created", id: 6001, message_type: "outgoing", private: false, content: "agent B", created_at: "2026-09-11T14:01:01.000Z", account: { id: 2 }, inbox: { id: 20 }, conversation: { id: convB.id }, sender: { id: 82, type: "user", name: "Agent B" }, attachments: [] };
if ((await sign(a, process.env.CHATWOOT_WEBHOOK_PHASE6_A_SECRET, payloadA)).data.status !== "delivered") throw new Error("Chatwoot A not delivered");
if ((await sign(b, process.env.CHATWOOT_WEBHOOK_PHASE6_B_SECRET, payloadB)).data.status !== "delivered") throw new Error("Chatwoot B not delivered");
if ((await sign(a, process.env.CHATWOOT_WEBHOOK_PHASE6_A_SECRET, payloadA)).data.status !== "duplicate") throw new Error("Chatwoot duplicate not suppressed");
const mismatch = await sign(a, process.env.CHATWOOT_WEBHOOK_PHASE6_A_SECRET, { ...payloadA, id: 6002, account: { id: 2 }, inbox: { id: 20 }, conversation: { id: convB.id } }, 409);
if (mismatch.error?.code !== "CHATWOOT_ROUTE_MISMATCH") throw new Error("cross-tenant route did not fail closed");
const matrix = await (await fetch("http://matrix-client-double:8082/_test/state")).json();
if (matrix.events.length !== 2 || matrix.events.filter(item => item.roomId === "!phase6-a:matrix.example.com").length !== 1 || matrix.events.filter(item => item.roomId === "!phase6-b:matrix.example.com").length !== 1) {
  throw new Error("Matrix A/B destinations crossed");
}
BUN

# Assigned proxy unavailable must fail at the assigned endpoint, never direct.
run_bun <<'BUN'
import { connect } from "node:net";
await fetch("http://proxy-a:8081/_test/down", { method: "POST" });
const response = await fetch("http://127.0.0.1:3000/internal/v1/egress/resolve?meta_account_id=meta-phase6-a&login_id=login-phase6-a&reason=proxy-failure&traffic_class=messaging", {
  headers: { authorization: `Bearer ${process.env.CONTROL_PLANE_INTERNAL_TOKEN}` },
});
if (!response.ok) throw new Error("resolver failed");
const proxy = new URL((await response.json()).proxy_url);
const result = await new Promise(resolve => {
  let data = "";
  const socket = connect(Number(proxy.port), proxy.hostname, () => socket.write("GET http://direct-egress-sentinel:8083/failure HTTP/1.1\r\nHost: direct-egress-sentinel:8083\r\nConnection: close\r\n\r\n"));
  socket.setTimeout(3000, () => socket.destroy());
  socket.on("data", chunk => data += chunk.toString());
  socket.on("error", () => resolve("transport-error"));
  socket.on("close", () => resolve(data));
});
if (!String(result).includes(" 502 ") && result !== "transport-error") throw new Error("unavailable proxy unexpectedly succeeded");
const direct = await (await fetch("http://direct-egress-sentinel:8083/_test/state")).json();
if (direct.hits !== 0) throw new Error("proxy failure fell back direct");
await fetch("http://proxy-a:8081/_test/up", { method: "POST" });
BUN

# Chatwoot timeout must be retryable with exactly the same source event identity.
run_bun <<'BUN'
const armed = await fetch("http://chatwoot-double:8080/_test/delay-next-message-create", {
  method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ ms: 9000 }),
});
if (!armed.ok) throw new Error("could not arm timeout");
BUN
run_bun <<'BUN'
const { Database } = await import("bun:sqlite");
const db = new Database("/data/control-plane.db", { readonly: true });
const row = db.query("select id from meta_connections where meta_account_id=?").get("meta-phase6-a");
db.close();
const event = { connectionId: row.id, roomId: "!phase6-a:matrix.example.com", remoteThreadId: "thread-phase6-a", remoteContactId: "contact-phase6-a", eventId: "$phase6-timeout-a", senderId: "contact-phase6-a", text: "timeout A", occurredAt: "2026-09-11T14:02:00.000Z", provenance: "meta" };
const response = await fetch("http://127.0.0.1:3000/internal/v1/matrix/events", {
  method: "POST", headers: { authorization: `Bearer ${process.env.CONTROL_PLANE_INTERNAL_TOKEN}`, "content-type": "application/json" }, body: JSON.stringify(event),
});
if (response.ok) throw new Error("timeout unexpectedly succeeded");
BUN
sleep 2
run_bun <<'BUN'
const { Database } = await import("bun:sqlite");
const db = new Database("/data/control-plane.db", { readonly: true });
const row = db.query("select id from meta_connections where meta_account_id=?").get("meta-phase6-a");
db.close();
const event = { connectionId: row.id, roomId: "!phase6-a:matrix.example.com", remoteThreadId: "thread-phase6-a", remoteContactId: "contact-phase6-a", eventId: "$phase6-timeout-a", senderId: "contact-phase6-a", text: "timeout A", occurredAt: "2026-09-11T14:02:00.000Z", provenance: "meta" };
const response = await fetch("http://127.0.0.1:3000/internal/v1/matrix/events", {
  method: "POST", headers: { authorization: `Bearer ${process.env.CONTROL_PLANE_INTERNAL_TOKEN}`, "content-type": "application/json" }, body: JSON.stringify(event),
});
const body = await response.json();
if (!response.ok || body.data?.status !== "delivered") throw new Error(`timeout retry failed ${response.status}`);
const state = await (await fetch("http://chatwoot-double:8080/_test/state")).json();
if (state.messages.filter(item => item.sourceEventId === "$phase6-timeout-a").length !== 1) throw new Error("timeout retry duplicated/lost");
BUN

# Restart must preserve identities, assignments and exact canonical duplicates.
"${DC[@]}" restart control-plane >/dev/null
wait_healthy control-plane 90
run_bun <<'BUN'
import { createHmac } from "node:crypto";
const { Database } = await import("bun:sqlite");
const db = new Database("/data/control-plane.db", { readonly: true });
const conns = db.query("select id,meta_account_id,mautrix_login_id from meta_connections where meta_account_id in (?,?) order by meta_account_id").all("meta-phase6-a", "meta-phase6-b");
const rows = db.query(`select cb.id binding_id, cb.chatwoot_account_id, cb.chatwoot_inbox_id,
  conv.chatwoot_conversation_id, conv.meta_connection_id
  from chatwoot_bindings cb join conversation_bindings conv
  on conv.tenant_id=cb.tenant_id and conv.chatwoot_account_id=cb.chatwoot_account_id and conv.chatwoot_inbox_id=cb.chatwoot_inbox_id
  where cb.chatwoot_inbox_id in (?,?) order by cb.chatwoot_inbox_id`).all("10", "20");
if (conns.length !== 2 || rows.length !== 2) throw new Error("restart lost routes");

const internal = { authorization: `Bearer ${process.env.CONTROL_PLANE_INTERNAL_TOKEN}`, "content-type": "application/json" };
for (const [index, connection] of conns.entries()) {
  const suffix = index ? "b" : "a";
  const event = { connectionId: connection.id, roomId: `!phase6-${suffix}:matrix.example.com`, remoteThreadId: `thread-phase6-${suffix}`, remoteContactId: `contact-phase6-${suffix}`, eventId: `$phase6-${suffix}-1`, senderId: `contact-phase6-${suffix}`, text: `from meta ${suffix.toUpperCase()}`, occurredAt: index ? "2026-09-11T14:00:01.000Z" : "2026-09-11T14:00:00.000Z", provenance: "meta" };
  const response = await fetch("http://127.0.0.1:3000/internal/v1/matrix/events", { method: "POST", headers: internal, body: JSON.stringify(event) });
  const body = await response.json();
  if (!response.ok || body.data?.status !== "duplicate") throw new Error(`Matrix replay ${suffix} not duplicate`);
  const query = new URLSearchParams({ meta_account_id: connection.meta_account_id, login_id: connection.mautrix_login_id, reason: "restart", traffic_class: "messaging" });
  const egress = await fetch(`http://127.0.0.1:3000/internal/v1/egress/resolve?${query}`, { headers: { authorization: `Bearer ${process.env.CONTROL_PLANE_INTERNAL_TOKEN}` } });
  const resolved = await egress.json();
  if (!egress.ok || new URL(resolved.proxy_url).hostname !== (suffix === "a" ? "proxy-a" : "proxy-b")) throw new Error(`egress changed ${suffix}`);
}

const payloads = [
  { event: "message_created", id: 6001, message_type: "outgoing", private: false, content: "agent A", created_at: "2026-09-11T14:01:00.000Z", account: { id: 1 }, inbox: { id: 10 }, conversation: { id: Number(rows[0].chatwoot_conversation_id) }, sender: { id: 81, type: "user", name: "Agent A" }, attachments: [] },
  { event: "message_created", id: 6001, message_type: "outgoing", private: false, content: "agent B", created_at: "2026-09-11T14:01:01.000Z", account: { id: 2 }, inbox: { id: 20 }, conversation: { id: Number(rows[1].chatwoot_conversation_id) }, sender: { id: 82, type: "user", name: "Agent B" }, attachments: [] },
];

function canonicalHash(payload) {
  const sender = payload.sender;
  return new Bun.CryptoHasher("sha256").update(JSON.stringify({
    messageId: String(payload.id),
    accountId: String(payload.account.id),
    inboxId: String(payload.inbox.id),
    conversationId: String(payload.conversation.id),
    senderId: String(sender.id),
    senderName: sender.name ?? null,
    text: payload.content ?? null,
    attachments: [],
    occurredAt: new Date(payload.created_at).toISOString(),
  })).digest("hex");
}

for (let index = 0; index < rows.length; index++) {
  const row = rows[index];
  const sourceEventId = `${row.binding_id}:6001`;
  const persisted = db.query("select payload_hash,meta_connection_id,status from processed_events where source='chatwoot' and source_event_id=?").get(sourceEventId);
  if (!persisted) throw new Error(`missing persisted webhook identity ${index}`);
  const expectedHash = canonicalHash(payloads[index]);
  if (persisted.payload_hash !== expectedHash) throw new Error(`persisted webhook hash mismatch ${index}: ${persisted.payload_hash} != ${expectedHash}`);
  if (persisted.meta_connection_id !== row.meta_connection_id) throw new Error(`persisted webhook connection mismatch ${index}`);
  if (persisted.status !== "delivered") throw new Error(`persisted webhook status mismatch ${index}: ${persisted.status}`);
}
db.close();

async function replay(row, secret, payload, expected = 200) {
  const raw = JSON.stringify(payload);
  const timestamp = String(Math.floor(Date.now() / 1000));
  const signature = "sha256=" + createHmac("sha256", secret).update(`${timestamp}.${raw}`).digest("hex");
  const response = await fetch(`http://127.0.0.1:3000/webhooks/chatwoot/${row.binding_id}`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-chatwoot-timestamp": timestamp, "x-chatwoot-signature": signature },
    body: raw,
  });
  const body = await response.json();
  if (response.status !== expected) throw new Error(`webhook replay status ${response.status}: ${JSON.stringify(body)}`);
  return body;
}
if ((await replay(rows[0], process.env.CHATWOOT_WEBHOOK_PHASE6_A_SECRET, payloads[0])).data?.status !== "duplicate") throw new Error("canonical A replay not duplicate");
if ((await replay(rows[1], process.env.CHATWOOT_WEBHOOK_PHASE6_B_SECRET, payloads[1])).data?.status !== "duplicate") throw new Error("canonical B replay not duplicate");
const reused = await replay(rows[0], process.env.CHATWOOT_WEBHOOK_PHASE6_A_SECRET, { ...payloads[0], content: "mutated reuse" }, 503);
if (reused.error?.code !== "EVENT_IDENTITY_CONFLICT") throw new Error("mutated event-ID reuse did not fail closed");

const chatwoot = await (await fetch("http://chatwoot-double:8080/_test/state")).json();
const matrix = await (await fetch("http://matrix-client-double:8082/_test/state")).json();
const direct = await (await fetch("http://direct-egress-sentinel:8083/_test/state")).json();
if (chatwoot.messages.length !== 3 || matrix.events.length !== 2 || direct.hits !== 0) throw new Error("restart changed side effects or direct-egress sentinel");
BUN

logs="$("${DC[@]}" logs --no-color control-plane chatwoot-double matrix-client-double proxy-a proxy-b direct-egress-sentinel 2>&1 || true)"
for secret in \
  "$CONTROL_PLANE_ADMIN_TOKEN" "$CONTROL_PLANE_INTERNAL_TOKEN" "$CHATWOOT_CI_TOKEN" \
  "$CHATWOOT_WEBHOOK_PHASE6_A_SECRET" "$CHATWOOT_WEBHOOK_PHASE6_B_SECRET" "$MATRIX_CLIENT_CI_TOKEN" \
  "$EGRESS_PROXY_PHASE6_A_PASSWORD" "$EGRESS_PROXY_PHASE6_B_PASSWORD"; do
  printf '%s' "$logs" | grep -Fq "$secret" && { echo "Synthetic Phase 6 secret leaked into service logs" >&2; exit 1; }
done

echo "Phase 6 multi-tenant production proof passed"
