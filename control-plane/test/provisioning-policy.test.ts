import { describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { createProvisioningApp } from "../src/provisioning-app";
import { runMigrations } from "../src/persistence/migrations";
import { SQLiteMetaConnectionRepository, SQLiteTenantRepository } from "../src/persistence/sqlite-repositories";

const ADMIN = "admin-token-123456789";
const INTERNAL = "internal-token-123456789012345";

describe("provisioning egress policy", () => {
  test("direct_allowed cannot issue a provisioning claim", async () => {
    const db = new Database(":memory:", { strict: true });
    try {
      runMigrations(db);
      const tenants = new SQLiteTenantRepository(db);
      const connections = new SQLiteMetaConnectionRepository(db);
      const tenant = tenants.create({ slug: "direct-policy", name: "Direct Policy" });
      const connection = connections.create({ tenantId: tenant.id, matrixOwnerMxid: "@owner:test", egressPolicy: "direct_allowed" });
      const app = createProvisioningApp(db, ADMIN, INTERNAL, {});
      const response = await app.handle(new Request(`http://localhost/api/v1/meta-connections/${connection.id}/provisioning-claims`, {
        method: "POST",
        headers: { authorization: `Bearer ${ADMIN}`, "content-type": "application/json" },
        body: "{}"
      }));
      expect(response.status).toBe(409);
      expect((await response.json()).error.code).toBe("PROXY_REQUIRED_FOR_PROVISIONING");
      const count = db.query("SELECT COUNT(*) AS count FROM provisioning_claims").get() as { count: number };
      expect(count.count).toBe(0);
    } finally {
      db.close();
    }
  });
});
