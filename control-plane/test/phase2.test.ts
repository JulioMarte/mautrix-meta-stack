import { afterEach, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { createApp } from "../src/app";
import { runMigrations } from "../src/persistence/migrations";
import { SQLiteAuditRepository, SQLiteEgressProfileRepository, SQLiteMetaConnectionRepository, SQLiteTenantRepository } from "../src/persistence/sqlite-repositories";
import { EnvironmentSecretProvider, redactUri } from "../src/security/secrets";
import { EgressResolver } from "../src/services/egress-resolver";

let db: Database | undefined;
afterEach(() => { db?.close(); db = undefined; });
function freshDb() { db = new Database(":memory:", { strict: true }); runMigrations(db); return db; }
function fixture() {
  const database = freshDb(); const tenants = new SQLiteTenantRepository(database); const connections = new SQLiteMetaConnectionRepository(database); const egress = new SQLiteEgressProfileRepository(database); const audit = new SQLiteAuditRepository(database);
  const tenantA = tenants.create({ slug: "tenant-a", name: "Tenant A" }); const tenantB = tenants.create({ slug: "tenant-b", name: "Tenant B" });
  const profileA = egress.create({ provider: "fixture", scheme: "socks5", host: "proxy-a.test", port: 1080, username: "alice", secretRef: "env:PROXY_A", country: "US", region: null, stickySessionId: "sticky-a", expectedExitIp: null, status: "healthy" });
  const profileB = egress.create({ provider: "fixture", scheme: "socks5", host: "proxy-b.test", port: 1080, username: "bob", secretRef: "env:PROXY_B", country: "US", region: null, stickySessionId: "sticky-b", expectedExitIp: null, status: "healthy" });
  const a = connections.create({ tenantId: tenantA.id, matrixOwnerMxid: "@a:test", metaAccountId: "meta-a" }); const b = connections.create({ tenantId: tenantB.id, matrixOwnerMxid: "@b:test", metaAccountId: "meta-b" });
  connections.assignEgress(a.id, profileA.id); connections.assignEgress(b.id, profileB.id); connections.setStatus(a.id, "active"); connections.setStatus(b.id, "active");
  return { connections, egress, audit, a, b, profileA, profileB, tenantA };
}

describe("Phase 2 resolver domain", () => {
  test("sticky assignments remain stable and two accounts resolve differently", () => {
    const f = fixture(); const resolver = new EgressResolver(f.connections, f.egress, new EnvironmentSecretProvider({ PROXY_A: "secret-a", PROXY_B: "secret-b" }));
    const a1 = resolver.resolve({ metaAccountId: "meta-a", reason: "connect", trafficClass: "messaging" });
    const a2 = resolver.resolve({ metaAccountId: "meta-a", reason: "reconnect", trafficClass: "media" });
    const b = resolver.resolve({ metaAccountId: "meta-b", reason: "connect", trafficClass: "messaging" });
    expect(a1.assignmentId).toBe(a2.assignmentId); expect(a1.assignmentId).not.toBe(b.assignmentId); expect(a1.proxyUrl).toContain("proxy-a.test"); expect(b.proxyUrl).toContain("proxy-b.test");
  });
  test("required egress fails closed when unhealthy or secret is missing", () => {
    const f = fixture(); f.egress.setStatus(f.profileA.id, "degraded"); const resolver = new EgressResolver(f.connections, f.egress, new EnvironmentSecretProvider({}));
    expect(() => resolver.resolve({ metaAccountId: "meta-a", reason: "connect", trafficClass: "messaging" })).toThrow("EGRESS_UNHEALTHY");
    f.egress.setStatus(f.profileA.id, "healthy");
    expect(() => resolver.resolve({ metaAccountId: "meta-a", reason: "connect", trafficClass: "messaging" })).toThrow("EGRESS_SECRET_MISSING");
  });
  test("explicit direct policy is never mistaken for an empty proxy success", () => {
    const f = fixture();
    const direct = f.connections.create({ tenantId: f.tenantA.id, matrixOwnerMxid: "@direct:test", metaAccountId: "meta-direct", egressPolicy: "direct_allowed" });
    f.connections.setStatus(direct.id, "active");
    const resolver = new EgressResolver(f.connections, f.egress, new EnvironmentSecretProvider({}));
    expect(() => resolver.resolve({ metaAccountId: "meta-direct", reason: "connect", trafficClass: "messaging" })).toThrow("DIRECT_EGRESS_NOT_SUPPORTED");
  });
  test("conflicting identity fails closed", () => {
    const f = fixture(); f.connections.setProviderIdentity(f.a.id, { mautrixLoginId: "login-a" }); f.connections.setProviderIdentity(f.b.id, { mautrixLoginId: "login-b" });
    const resolver = new EgressResolver(f.connections, f.egress, new EnvironmentSecretProvider({ PROXY_A: "a", PROXY_B: "b" }));
    expect(() => resolver.resolve({ metaAccountId: "meta-a", loginId: "login-b", reason: "reconnect", trafficClass: "messaging" })).toThrow("IDENTITY_CONFLICT");
  });
  test("redaction removes proxy credentials", () => { expect(redactUri("socks5://user:canary-secret@proxy.test:1080")).not.toContain("canary-secret"); });
});

describe("Phase 2 internal API", () => {
  test("resolver requires internal authentication and returns confidential proxy only when authorized", async () => {
    const f = fixture(); const app = createApp(db!, "admin-token-123456789", "internal-token-123456789012345", { PROXY_A: "canary-secret", PROXY_B: "secret-b" });
    const url = "http://localhost/internal/v1/egress/resolve?meta_account_id=meta-a&reason=connect&traffic_class=messaging";
    expect((await app.handle(new Request(url))).status).toBe(401);
    const response = await app.handle(new Request(url, { headers: { authorization: "Bearer internal-token-123456789012345" } }));
    expect(response.status).toBe(200); const body = await response.json(); expect(body.proxy_url).toContain("canary-secret"); expect(body.assignment_id).toBe(f.profileA.id);
  });
  test("invalid status cannot silently create a healthy egress", async () => {
    freshDb(); const app = createApp(db!, "admin-token-123456789", "internal-token-123456789012345", {});
    const response = await app.handle(new Request("http://localhost/api/v1/egress-profiles", { method: "POST", headers: { authorization: "Bearer admin-token-123456789", "content-type": "application/json" }, body: JSON.stringify({ provider: "fixture", scheme: "socks5", host: "proxy.test", port: 1080, status: "disabledd" }) }));
    expect(response.status).toBe(400); expect((await response.json()).error.code).toBe("INVALID_EGRESS_STATUS");
  });
  test("assignment management is audited without secret material", async () => {
    const database = freshDb(); const tenants = new SQLiteTenantRepository(database); const connections = new SQLiteMetaConnectionRepository(database); const egress = new SQLiteEgressProfileRepository(database); const audit = new SQLiteAuditRepository(database);
    const tenant = tenants.create({ slug: "audit", name: "Audit" }); const connection = connections.create({ tenantId: tenant.id, matrixOwnerMxid: "@a:test", metaAccountId: "meta-a" });
    const profile = egress.create({ provider: "fixture", scheme: "socks5", host: "proxy.test", port: 1080, username: "u", secretRef: "env:CANARY", country: null, region: null, stickySessionId: null, expectedExitIp: null, status: "healthy" });
    const app = createApp(database, "admin-token-123456789", "internal-token-123456789012345", { CANARY: "raw-secret-canary" });
    const response = await app.handle(new Request(`http://localhost/api/v1/meta-connections/${connection.id}/egress`, { method: "POST", headers: { authorization: "Bearer admin-token-123456789", "content-type": "application/json" }, body: JSON.stringify({ egressProfileId: profile.id }) }));
    expect(response.status).toBe(200); const events = audit.listForEntity("meta_connection", connection.id); expect(events).toHaveLength(1); expect(JSON.stringify(events)).not.toContain("raw-secret-canary");
  });
});
