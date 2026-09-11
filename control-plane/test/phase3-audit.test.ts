import { afterEach, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { createApp } from "../src/app";
import { runMigrations } from "../src/persistence/migrations";
import { SQLiteEgressProfileRepository, SQLiteMetaConnectionRepository, SQLiteTenantRepository } from "../src/persistence/sqlite-repositories";
import { EnvironmentSecretProvider } from "../src/security/secrets";
import { EgressResolver } from "../src/services/egress-resolver";

let db: Database | undefined;
afterEach(() => { db?.close(); db = undefined; });

function freshDb() {
  db = new Database(":memory:", { strict: true });
  runMigrations(db);
  return db;
}

function activeConnection(metaAccountId: string, mautrixLoginId?: string) {
  const database = db ?? freshDb();
  const tenants = new SQLiteTenantRepository(database);
  const connections = new SQLiteMetaConnectionRepository(database);
  const egress = new SQLiteEgressProfileRepository(database);
  const tenant = tenants.create({ slug: `tenant-${metaAccountId}`, name: `Tenant ${metaAccountId}` });
  const profile = egress.create({ provider: "fixture", scheme: "http", host: "proxy.test", port: 8080, username: null, secretRef: null, country: null, region: null, stickySessionId: null, expectedExitIp: null, status: "healthy" });
  const connection = connections.create({ tenantId: tenant.id, matrixOwnerMxid: `@${metaAccountId}:test`, metaAccountId, ...(mautrixLoginId ? { mautrixLoginId } : {}) });
  connections.assignEgress(connection.id, profile.id);
  connections.setStatus(connection.id, "active");
  return { connections, egress, connection, profile };
}

describe("Phase 3 audit hardening", () => {
  test("prebound Meta identity accepts the deterministic login ID before it is persisted", () => {
    const fixture = activeConnection("123");
    const resolver = new EgressResolver(fixture.connections, fixture.egress, new EnvironmentSecretProvider({}));
    const result = resolver.resolve({ metaAccountId: "123", loginId: "123", reason: "connect", trafficClass: "messaging" });
    expect(result.connectionId).toBe(fixture.connection.id);
    expect(result.assignmentId).toBe(fixture.profile.id);
  });

  test("two supplied identities that point at different active connections fail closed", () => {
    const first = activeConnection("meta-a", "login-a");
    activeConnection("meta-b", "login-b");
    const resolver = new EgressResolver(first.connections, first.egress, new EnvironmentSecretProvider({}));
    expect(() => resolver.resolve({ metaAccountId: "meta-a", loginId: "login-b", reason: "connect", trafficClass: "messaging" })).toThrow("IDENTITY_CONFLICT");
  });

  test("management API rejects unusable proxy profiles before persistence", async () => {
    const database = freshDb();
    const app = createApp(database, "admin-token-123456789", "internal-token-123456789012345", {});
    const headers = { authorization: "Bearer admin-token-123456789", "content-type": "application/json" };
    for (const body of [
      { provider: "fixture", scheme: "ftp", host: "proxy.test", port: 21 },
      { provider: "fixture", scheme: "http", host: "proxy.test/path", port: 8080 },
      { provider: "fixture", scheme: "http", host: "proxy.test", port: 8080, username: "user" },
      { provider: "fixture", scheme: "http", host: "proxy.test", port: 8080, secretRef: "env:PROXY_SECRET" },
    ]) {
      const response = await app.handle(new Request("http://localhost/api/v1/egress-profiles", { method: "POST", headers, body: JSON.stringify(body) }));
      expect(response.status).toBe(400);
    }
    expect(new SQLiteEgressProfileRepository(database).list()).toHaveLength(0);
  });
});
