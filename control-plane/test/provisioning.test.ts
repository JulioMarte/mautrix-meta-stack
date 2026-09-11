import { afterEach, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { createProvisioningApp } from "../src/provisioning-app";
import { runMigrations } from "../src/persistence/migrations";
import { SQLiteEgressProfileRepository, SQLiteMetaConnectionRepository, SQLiteTenantRepository } from "../src/persistence/sqlite-repositories";

const ADMIN = "admin-token-123456789";
const INTERNAL = "internal-token-123456789012345";
let db: Database | undefined;
afterEach(() => { db?.close(); db = undefined; });

function freshDb() {
  db = new Database(":memory:", { strict: true });
  runMigrations(db);
  return db;
}

function fixture(options: { sameOwner?: boolean } = {}) {
  const database = freshDb();
  const tenants = new SQLiteTenantRepository(database);
  const connections = new SQLiteMetaConnectionRepository(database);
  const egress = new SQLiteEgressProfileRepository(database);
  const tenantA = tenants.create({ slug: "claim-a", name: "Claim A" });
  const tenantB = tenants.create({ slug: "claim-b", name: "Claim B" });
  const profileA = egress.create({ provider: "fixture", scheme: "socks5", host: "proxy-a.test", port: 1080, username: "a", secretRef: "env:EGRESS_PROXY_A", country: null, region: null, stickySessionId: null, expectedExitIp: null, status: "healthy" });
  const profileB = egress.create({ provider: "fixture", scheme: "socks5", host: "proxy-b.test", port: 1080, username: "b", secretRef: "env:EGRESS_PROXY_B", country: null, region: null, stickySessionId: null, expectedExitIp: null, status: "healthy" });
  const ownerA = "@owner:test";
  const ownerB = options.sameOwner ? ownerA : "@other:test";
  const a = connections.create({ tenantId: tenantA.id, matrixOwnerMxid: ownerA });
  const b = connections.create({ tenantId: tenantB.id, matrixOwnerMxid: ownerB });
  connections.assignEgress(a.id, profileA.id);
  connections.assignEgress(b.id, profileB.id);
  const app = createProvisioningApp(database, ADMIN, INTERNAL, { EGRESS_PROXY_A: "secret-a", EGRESS_PROXY_B: "secret-b" });
  return { database, connections, egress, tenantA, tenantB, profileA, profileB, a, b, ownerA, ownerB, app };
}

async function post(app: ReturnType<typeof createProvisioningApp>, path: string, body: unknown, token = ADMIN) {
  return app.handle(new Request(`http://localhost${path}`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify(body)
  }));
}

async function issue(f: ReturnType<typeof fixture>, connectionId: string, ttlSeconds = 600) {
  const response = await post(f.app, `/api/v1/meta-connections/${connectionId}/provisioning-claims`, { ttlSeconds });
  expect(response.status).toBe(201);
  return (await response.json()).data as { claim: string; claimId: string; metaConnectionId: string; expiresAt: string };
}

async function consume(f: ReturnType<typeof fixture>, claim: string, metaAccountId: string, matrixOwnerMxid: string) {
  return post(f.app, "/internal/v1/provisioning/consume", { claim, metaAccountId, matrixOwnerMxid }, INTERNAL);
}

describe("provisioning claims", () => {
  test("issue stores only a digest and valid consume binds c_user before returning bootstrap egress", async () => {
    const f = fixture();
    const issued = await issue(f, f.a.id);
    const stored = f.database.query("SELECT secret_digest, used_at, revoked_at FROM provisioning_claims WHERE id=?").get(issued.claimId) as Record<string, unknown>;
    expect(stored.secret_digest).not.toBe(issued.claim);
    expect(String(stored.secret_digest)).toHaveLength(64);
    expect(JSON.stringify(stored)).not.toContain(issued.claim);

    const response = await consume(f, issued.claim, "123456789", f.ownerA);
    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body.connection_id).toBe(f.a.id);
    expect(body.meta_account_id).toBe("123456789");
    expect(body.status).toBe("ready");
    expect(body.assignment_id).toBe(f.profileA.id);
    expect(body.proxy_url).toContain("proxy-a.test");
    expect(body.proxy_url).toContain("secret-a");

    const connection = f.connections.findById(f.a.id)!;
    expect(connection.metaAccountId).toBe("123456789");
    expect(connection.status).toBe("ready");
    const used = f.database.query("SELECT used_at FROM provisioning_claims WHERE id=?").get(issued.claimId) as { used_at: string | null };
    expect(used.used_at).not.toBeNull();
    expect((await consume(f, issued.claim, "123456789", f.ownerA)).status).toBe(409);
  });

  test("forged, expired, revoked and wrong-owner claims fail closed without binding identity", async () => {
    const f = fixture();
    expect((await consume(f, "pc_00000000-0000-0000-0000-000000000000.fakefakefakefakefakefakefakefakefakefake", "123456789", f.ownerA)).status).toBe(409);

    const expired = await issue(f, f.a.id);
    f.database.query("UPDATE provisioning_claims SET expires_at=? WHERE id=?").run("2000-01-01T00:00:00.000Z", expired.claimId);
    let response = await consume(f, expired.claim, "123456789", f.ownerA);
    expect(response.status).toBe(409);
    expect((await response.json()).error.code).toBe("PROVISIONING_CLAIM_EXPIRED");

    const wrongOwner = await issue(f, f.a.id);
    response = await consume(f, wrongOwner.claim, "123456789", "@attacker:test");
    expect(response.status).toBe(409);
    expect((await response.json()).error.code).toBe("PROVISIONING_OWNER_MISMATCH");

    const revoked = await issue(f, f.a.id);
    const revoke = await post(f.app, `/api/v1/meta-connections/${f.a.id}/provisioning-claims/${revoked.claimId}/revoke`, {});
    expect(revoke.status).toBe(200);
    response = await consume(f, revoked.claim, "123456789", f.ownerA);
    expect(response.status).toBe(409);
    expect((await response.json()).error.code).toBe("PROVISIONING_CLAIM_REVOKED");
    expect(f.connections.findById(f.a.id)!.metaAccountId).toBeNull();
  });

  test("one Matrix owner can provision two connections without ambiguity or tenant crossover", async () => {
    const f = fixture({ sameOwner: true });
    const claimA = await issue(f, f.a.id);
    const claimB = await issue(f, f.b.id);
    const a = await consume(f, claimA.claim, "111111111", f.ownerA);
    const b = await consume(f, claimB.claim, "222222222", f.ownerA);
    expect(a.status).toBe(200);
    expect(b.status).toBe(200);
    expect((await a.json()).connection_id).toBe(f.a.id);
    expect((await b.json()).connection_id).toBe(f.b.id);
    expect(f.connections.findById(f.a.id)!.tenantId).toBe(f.tenantA.id);
    expect(f.connections.findById(f.b.id)!.tenantId).toBe(f.tenantB.id);
    expect(f.connections.findById(f.a.id)!.metaAccountId).toBe("111111111");
    expect(f.connections.findById(f.b.id)!.metaAccountId).toBe("222222222");
  });

  test("conflicting Meta identity cannot be rebound or stolen by another connection", async () => {
    const f = fixture();
    f.connections.setProviderIdentity(f.b.id, { metaAccountId: "333333333" });
    const claim = await issue(f, f.a.id);
    const response = await consume(f, claim.claim, "333333333", f.ownerA);
    expect(response.status).toBe(409);
    expect((await response.json()).error.code).toBe("META_ACCOUNT_CONFLICT");
    expect(f.connections.findById(f.a.id)!.metaAccountId).toBeNull();
  });

  test("re-login preserves same account but blocks silent account replacement", async () => {
    const f = fixture();
    f.connections.setProviderIdentity(f.a.id, { metaAccountId: "444444444" });
    f.connections.setStatus(f.a.id, "active");
    const same = await issue(f, f.a.id);
    expect((await consume(f, same.claim, "444444444", f.ownerA)).status).toBe(200);
    expect(f.connections.findById(f.a.id)!.status).toBe("active");

    const different = await issue(f, f.a.id);
    const response = await consume(f, different.claim, "555555555", f.ownerA);
    expect(response.status).toBe(409);
    expect((await response.json()).error.code).toBe("META_ACCOUNT_CONFLICT");
    expect(f.connections.findById(f.a.id)!.metaAccountId).toBe("444444444");
  });

  test("bind-login attaches BridgeV2 login ID to the exact consumed connection and rejects conflicts", async () => {
    const f = fixture();
    const claim = await issue(f, f.a.id);
    expect((await consume(f, claim.claim, "666666666", f.ownerA)).status).toBe(200);
    let response = await post(f.app, "/internal/v1/provisioning/bind-login", {
      connectionId: f.a.id, metaAccountId: "666666666", loginId: "facebook_666666666", matrixOwnerMxid: f.ownerA
    }, INTERNAL);
    expect(response.status).toBe(200);
    expect(f.connections.findById(f.a.id)!.mautrixLoginId).toBe("facebook_666666666");

    response = await post(f.app, "/internal/v1/provisioning/bind-login", {
      connectionId: f.a.id, metaAccountId: "666666666", loginId: "facebook_other", matrixOwnerMxid: f.ownerA
    }, INTERNAL);
    expect(response.status).toBe(409);
    expect((await response.json()).error.code).toBe("MAUTRIX_LOGIN_CONFLICT");
  });

  test("claim issuance requires eligible healthy assigned egress", async () => {
    const f = fixture();
    f.egress.setStatus(f.profileA.id, "degraded");
    let response = await post(f.app, `/api/v1/meta-connections/${f.a.id}/provisioning-claims`, {});
    expect(response.status).toBe(409);
    expect((await response.json()).error.code).toBe("EGRESS_UNHEALTHY");
    f.egress.setStatus(f.profileA.id, "healthy");
    f.connections.setStatus(f.a.id, "disabled");
    response = await post(f.app, `/api/v1/meta-connections/${f.a.id}/provisioning-claims`, {});
    expect(response.status).toBe(409);
    expect((await response.json()).error.code).toBe("CONNECTION_NOT_PROVISIONABLE");
  });

  test("audit/persistence contain no raw claim or proxy secret", async () => {
    const f = fixture();
    const issued = await issue(f, f.a.id);
    const response = await consume(f, issued.claim, "777777777", f.ownerA);
    expect(response.status).toBe(200);
    const claims = f.database.query("SELECT * FROM provisioning_claims").all();
    const audits = f.database.query("SELECT * FROM audit_events").all();
    const serialized = JSON.stringify({ claims, audits });
    expect(serialized).not.toContain(issued.claim);
    expect(serialized).not.toContain("secret-a");
    expect(serialized).not.toContain("secret-b");
    expect(serialized).not.toContain("c_user");
  });
});
