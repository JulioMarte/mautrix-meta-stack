import { afterEach, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { runMigrations } from "../src/persistence/migrations";
import { SQLiteEgressProfileRepository, SQLiteMetaConnectionRepository, SQLiteTenantRepository } from "../src/persistence/sqlite-repositories";
import { EnvironmentSecretProvider } from "../src/security/secrets";
import { EgressResolver } from "../src/services/egress-resolver";

let db: Database | undefined;
afterEach(() => { db?.close(); db = undefined; });

function resolverWithScheme(scheme: string) {
  db = new Database(":memory:", { strict: true });
  runMigrations(db);
  const tenants = new SQLiteTenantRepository(db);
  const connections = new SQLiteMetaConnectionRepository(db);
  const egress = new SQLiteEgressProfileRepository(db);
  const tenant = tenants.create({ slug: "hardening", name: "Hardening" });
  const profile = egress.create({ provider: "fixture", scheme, host: "proxy.test", port: 1080, username: null, secretRef: null, country: null, region: null, stickySessionId: null, expectedExitIp: null, status: "healthy" });
  const connection = connections.create({ tenantId: tenant.id, matrixOwnerMxid: "@hardening:test", metaAccountId: "meta-hardening" });
  connections.assignEgress(connection.id, profile.id);
  connections.setStatus(connection.id, "active");
  return new EgressResolver(connections, egress, new EnvironmentSecretProvider({}));
}

describe("Phase 3 egress hardening", () => {
  test("supported proxy schemes remain resolvable", () => {
    for (const scheme of ["http", "https", "socks5"]) {
      const resolver = resolverWithScheme(scheme);
      expect(resolver.resolve({ metaAccountId: "meta-hardening", reason: "connect", trafficClass: "messaging" }).proxyUrl).toStartWith(`${scheme}://`);
      db?.close(); db = undefined;
    }
  });

  test("unsupported URI schemes fail closed", () => {
    const resolver = resolverWithScheme("ftp");
    expect(() => resolver.resolve({ metaAccountId: "meta-hardening", reason: "connect", trafficClass: "messaging" })).toThrow("EGRESS_CONFIGURATION_INVALID");
  });
});
