import { afterEach, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { createApp } from "../src/app";
import { LATEST_SCHEMA_VERSION, runMigrations, schemaVersion } from "../src/persistence/migrations";
import { SQLiteMetaConnectionRepository, SQLiteTenantRepository } from "../src/persistence/sqlite-repositories";

let db: Database | undefined;
afterEach(() => { db?.close(); db = undefined; });

function freshDb() {
  db = new Database(":memory:", { strict: true });
  runMigrations(db);
  return db;
}

describe("Phase 1 persistence", () => {
  test("migrations create and remain idempotent", () => {
    const database = freshDb();
    expect(schemaVersion(database)).toBe(LATEST_SCHEMA_VERSION);
    runMigrations(database);
    expect(schemaVersion(database)).toBe(LATEST_SCHEMA_VERSION);
  });

  test("repositories persist tenant and configuration records", () => {
    const database = freshDb();
    const tenants = new SQLiteTenantRepository(database);
    const connections = new SQLiteMetaConnectionRepository(database);
    const tenant = tenants.create({ slug: "tenant-a", name: "Tenant A" });
    const connection = connections.create({ tenantId: tenant.id, matrixOwnerMxid: "@owner:matrix.example.com" });
    expect(tenants.findById(tenant.id)?.slug).toBe("tenant-a");
    expect(connections.findById(connection.id)?.tenantId).toBe(tenant.id);
    expect(connection.egressPolicy).toBe("proxy_required");
    expect(connection.status).toBe("draft");
  });
});

describe("Phase 1 HTTP surface", () => {
  test("liveness and readiness are distinct and healthy after migrations", async () => {
    const app = createApp(freshDb(), "test-admin-token-123456");
    expect((await app.handle(new Request("http://localhost/health/live"))).status).toBe(200);
    const ready = await app.handle(new Request("http://localhost/health/ready"));
    expect(ready.status).toBe(200);
    expect(await ready.json()).toMatchObject({ status: "ready", schemaVersion: LATEST_SCHEMA_VERSION });
  });

  test("admin and management API reject unauthorized access", async () => {
    const app = createApp(freshDb(), "test-admin-token-123456");
    expect((await app.handle(new Request("http://localhost/admin"))).status).toBe(401);
    expect((await app.handle(new Request("http://localhost/api/v1/tenants"))).status).toBe(401);
  });

  test("authorized management API can create a tenant", async () => {
    const app = createApp(freshDb(), "test-admin-token-123456");
    const response = await app.handle(new Request("http://localhost/api/v1/tenants", {
      method: "POST",
      headers: { authorization: "Bearer test-admin-token-123456", "content-type": "application/json" },
      body: JSON.stringify({ slug: "tenant-a", name: "Tenant A" })
    }));
    expect(response.status).toBe(201);
    expect((await response.json()).data.slug).toBe("tenant-a");
  });
});
