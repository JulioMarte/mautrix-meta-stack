import { afterEach, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { runMigrations } from "../src/persistence/migrations";
import { SQLiteEgressProfileRepository, SQLiteMetaConnectionRepository, SQLiteTenantRepository } from "../src/persistence/sqlite-repositories";
import { EnvironmentSecretProvider } from "../src/security/secrets";
import { EgressResolver, normalizeProxyHost } from "../src/services/egress-resolver";

let db: Database | undefined;
afterEach(() => { db?.close(); db = undefined; });

function resolverWithEndpoint(scheme: string, host = "proxy.test") {
  db = new Database(":memory:", { strict: true });
  runMigrations(db);
  const tenants = new SQLiteTenantRepository(db);
  const connections = new SQLiteMetaConnectionRepository(db);
  const egress = new SQLiteEgressProfileRepository(db);
  const tenant = tenants.create({ slug: "hardening", name: "Hardening" });
  const profile = egress.create({ provider: "fixture", scheme, host, port: 1080, username: null, secretRef: null, country: null, region: null, stickySessionId: null, expectedExitIp: null, status: "healthy" });
  const connection = connections.create({ tenantId: tenant.id, matrixOwnerMxid: "@hardening:test", metaAccountId: "meta-hardening" });
  connections.assignEgress(connection.id, profile.id);
  connections.setStatus(connection.id, "active");
  return new EgressResolver(connections, egress, new EnvironmentSecretProvider({}));
}

const resolve = (resolver: EgressResolver) => resolver.resolve({ metaAccountId: "meta-hardening", reason: "connect", trafficClass: "messaging" });

describe("Phase 3 egress hardening", () => {
  test("supported proxy schemes remain resolvable", () => {
    for (const scheme of ["http", "https", "socks5"]) {
      const resolver = resolverWithEndpoint(scheme);
      expect(resolve(resolver).proxyUrl).toStartWith(`${scheme}://`);
      db?.close(); db = undefined;
    }
  });

  test("unsupported URI schemes fail closed", () => {
    expect(() => resolve(resolverWithEndpoint("ftp"))).toThrow("EGRESS_CONFIGURATION_INVALID");
  });

  test("DNS and IP hosts normalize without changing authority", () => {
    expect(normalizeProxyHost("Proxy-01.Example.COM")).toBe("proxy-01.example.com");
    expect(normalizeProxyHost("192.0.2.10")).toBe("192.0.2.10");
    expect(normalizeProxyHost("2001:db8::10")).toBe("[2001:db8::10]");
    expect(resolve(resolverWithEndpoint("http", "2001:db8::10")).proxyUrl).toBe("http://[2001:db8::10]:1080");
  });

  test("authority-changing or malformed hosts fail closed", () => {
    for (const host of [" proxy.test", "proxy.test ", "proxy.test/path", "proxy.test@evil.test", "proxy.test?x=1", "-proxy.test", "proxy..test", "[2001:db8::10]"]) {
      expect(normalizeProxyHost(host)).toBeNull();
      expect(() => resolve(resolverWithEndpoint("http", host))).toThrow("EGRESS_CONFIGURATION_INVALID");
      db?.close(); db = undefined;
    }
  });
});
