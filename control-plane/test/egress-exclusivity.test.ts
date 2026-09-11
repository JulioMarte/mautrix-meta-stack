import { afterEach, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { runMigrations } from "../src/persistence/migrations";
import {
  SQLiteEgressProfileRepository,
  SQLiteMetaConnectionRepository,
  SQLiteTenantRepository
} from "../src/persistence/sqlite-repositories";

let db: Database | undefined;
afterEach(() => { db?.close(); db = undefined; });

function setup() {
  db = new Database(":memory:", { strict: true });
  runMigrations(db);
  const tenants = new SQLiteTenantRepository(db);
  const connections = new SQLiteMetaConnectionRepository(db);
  const egress = new SQLiteEgressProfileRepository(db);
  const tenantA = tenants.create({ slug: "egress-a", name: "Egress A" });
  const tenantB = tenants.create({ slug: "egress-b", name: "Egress B" });
  const connectionA = connections.create({ tenantId: tenantA.id, matrixOwnerMxid: "@a:matrix.example.com" });
  const connectionB = connections.create({ tenantId: tenantB.id, matrixOwnerMxid: "@b:matrix.example.com" });
  const profileA = egress.create({
    provider: "test",
    scheme: "http",
    host: "proxy-a.example.test",
    port: 8080,
    username: null,
    secretRef: null,
    country: null,
    region: null,
    stickySessionId: null,
    expectedExitIp: null,
    status: "healthy"
  });
  const profileB = egress.create({
    provider: "test",
    scheme: "http",
    host: "proxy-b.example.test",
    port: 8081,
    username: null,
    secretRef: null,
    country: null,
    region: null,
    stickySessionId: null,
    expectedExitIp: null,
    status: "healthy"
  });
  return { connections, connectionA, connectionB, profileA, profileB };
}

describe("exclusive live egress reservations", () => {
  test("two non-disabled connections cannot reserve the same egress profile", () => {
    const { connections, connectionA, connectionB, profileA } = setup();
    connections.assignEgress(connectionA.id, profileA.id);
    expect(() => connections.assignEgress(connectionB.id, profileA.id)).toThrow();
    expect(connections.findById(connectionB.id)?.egressProfileId).toBeNull();
  });

  test("a disabled connection releases its reservation, but cannot reactivate after reuse", () => {
    const { connections, connectionA, connectionB, profileA } = setup();
    connections.assignEgress(connectionA.id, profileA.id);
    connections.setStatus(connectionA.id, "disabled");
    expect(connections.assignEgress(connectionB.id, profileA.id).egressProfileId).toBe(profileA.id);
    expect(() => connections.setStatus(connectionA.id, "active")).toThrow();
    expect(connections.findById(connectionA.id)?.status).toBe("disabled");
  });

  test("distinct connections can reserve distinct profiles", () => {
    const { connections, connectionA, connectionB, profileA, profileB } = setup();
    expect(connections.assignEgress(connectionA.id, profileA.id).egressProfileId).toBe(profileA.id);
    expect(connections.assignEgress(connectionB.id, profileB.id).egressProfileId).toBe(profileB.id);
  });
});
