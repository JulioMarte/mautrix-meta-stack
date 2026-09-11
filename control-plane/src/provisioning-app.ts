import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import type { Database } from "bun:sqlite";
import { Elysia } from "elysia";
import { EnvironmentSecretProvider } from "./security/secrets";
import { normalizeProxyHost, normalizeProxyScheme } from "./services/egress-resolver";

const DEFAULT_TTL_SECONDS = 600;
const MIN_TTL_SECONDS = 60;
const MAX_TTL_SECONDS = 1800;
const META_ACCOUNT_RE = /^[1-9][0-9]{2,31}$/;
const LOGIN_ID_RE = /^[A-Za-z0-9._:@+-]{1,255}$/;

function bearerToken(request: Request): string | null {
  const value = request.headers.get("authorization");
  if (!value?.startsWith("Bearer ")) return null;
  return value.slice(7);
}

function tokenMatches(actual: string | null, expected: string, minimumLength: number): boolean {
  if (!actual || expected.length < minimumLength) return false;
  const left = Buffer.from(actual);
  const right = Buffer.from(expected);
  return left.length === right.length && timingSafeEqual(left, right);
}

function fail(set: { status?: number | string }, status: number, code: string, message: string) {
  set.status = status;
  return { error: { code, message } };
}

function digestClaim(raw: string): string {
  return createHash("sha256").update(raw, "utf8").digest("hex");
}

function issueRawClaim(id: string): string {
  return `pc_${id}.${randomBytes(32).toString("base64url")}`;
}

type ProvisioningRow = {
  claim_id: string;
  secret_digest: string;
  meta_connection_id: string;
  claim_tenant_id: string;
  claim_owner_mxid: string;
  expires_at: string;
  used_at: string | null;
  revoked_at: string | null;
  connection_tenant_id: string;
  connection_owner_mxid: string;
  meta_account_id: string | null;
  mautrix_login_id: string | null;
  egress_profile_id: string | null;
  egress_policy: string;
  connection_status: string;
  tenant_status: string;
  egress_scheme: string | null;
  egress_host: string | null;
  egress_port: number | null;
  egress_username: string | null;
  egress_secret_ref: string | null;
  egress_status: string | null;
};

function readClaim(db: Database, digest: string): ProvisioningRow | null {
  return db.query(`
    SELECT
      pc.id AS claim_id,
      pc.secret_digest,
      pc.meta_connection_id,
      pc.tenant_id AS claim_tenant_id,
      pc.matrix_owner_mxid AS claim_owner_mxid,
      pc.expires_at,
      pc.used_at,
      pc.revoked_at,
      mc.tenant_id AS connection_tenant_id,
      mc.matrix_owner_mxid AS connection_owner_mxid,
      mc.meta_account_id,
      mc.mautrix_login_id,
      mc.egress_profile_id,
      mc.egress_policy,
      mc.status AS connection_status,
      t.status AS tenant_status,
      ep.scheme AS egress_scheme,
      ep.host AS egress_host,
      ep.port AS egress_port,
      ep.username AS egress_username,
      ep.secret_ref AS egress_secret_ref,
      ep.status AS egress_status
    FROM provisioning_claims pc
    JOIN meta_connections mc ON mc.id = pc.meta_connection_id
    JOIN tenants t ON t.id = mc.tenant_id
    LEFT JOIN egress_profiles ep ON ep.id = mc.egress_profile_id
    WHERE pc.secret_digest = ?
  `).get(digest) as ProvisioningRow | null;
}

function assignedProxyUrl(row: ProvisioningRow, secrets: EnvironmentSecretProvider): { proxyUrl: string; assignmentId: string } {
  if (!row.egress_profile_id) throw new Error("EGRESS_ASSIGNMENT_REQUIRED");
  if (row.egress_status !== "healthy") throw new Error("EGRESS_UNHEALTHY");
  const scheme = normalizeProxyScheme(row.egress_scheme ?? "");
  const host = normalizeProxyHost(row.egress_host ?? "");
  const port = Number(row.egress_port ?? 0);
  if (!scheme || !host || port < 1 || port > 65535) throw new Error("EGRESS_CONFIGURATION_INVALID");

  let auth = "";
  if (row.egress_secret_ref) {
    const secret = secrets.resolve(row.egress_secret_ref);
    if (!secret) throw new Error("EGRESS_SECRET_MISSING");
    if (!row.egress_username) throw new Error("EGRESS_CONFIGURATION_INVALID");
    auth = `${encodeURIComponent(row.egress_username)}:${encodeURIComponent(secret)}@`;
  } else if (row.egress_username) {
    throw new Error("EGRESS_CONFIGURATION_INVALID");
  }
  return { proxyUrl: `${scheme}://${auth}${host}:${port}`, assignmentId: row.egress_profile_id };
}

function recordAudit(
  db: Database,
  input: { tenantId: string; actorType: string; actorId: string; action: string; entityType: string; entityId: string; before?: unknown; after?: unknown }
) {
  db.query(`INSERT INTO audit_events(id, tenant_id, actor_type, actor_id, action, entity_type, entity_id, before_json, after_json, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
    .run(
      crypto.randomUUID(), input.tenantId, input.actorType, input.actorId, input.action,
      input.entityType, input.entityId,
      input.before === undefined ? null : JSON.stringify(input.before),
      input.after === undefined ? null : JSON.stringify(input.after),
      new Date().toISOString()
    );
}

export function createProvisioningApp(
  db: Database,
  adminToken: string,
  internalToken: string,
  secretEnv: Record<string, string | undefined> = process.env
) {
  const secrets = new EnvironmentSecretProvider(secretEnv);
  const adminAuthorized = (request: Request) => tokenMatches(bearerToken(request), adminToken, 16);
  const internalAuthorized = (request: Request) => tokenMatches(bearerToken(request), internalToken, 24);

  return new Elysia({ name: "provisioning-claims" })
    .post("/api/v1/meta-connections/:id/provisioning-claims", ({ request, params, body, set }) => {
      if (!adminAuthorized(request)) return fail(set, 401, "UNAUTHORIZED", "Authentication required");
      const input = (body ?? {}) as { ttlSeconds?: unknown };
      const ttlSeconds = input.ttlSeconds === undefined ? DEFAULT_TTL_SECONDS : input.ttlSeconds;
      if (typeof ttlSeconds !== "number" || !Number.isInteger(ttlSeconds) || ttlSeconds < MIN_TTL_SECONDS || ttlSeconds > MAX_TTL_SECONDS) {
        return fail(set, 400, "INVALID_PROVISIONING_TTL", `ttlSeconds must be between ${MIN_TTL_SECONDS} and ${MAX_TTL_SECONDS}`);
      }

      const connection = db.query(`
        SELECT mc.*, t.status AS tenant_status, ep.status AS egress_status
        FROM meta_connections mc
        JOIN tenants t ON t.id = mc.tenant_id
        LEFT JOIN egress_profiles ep ON ep.id = mc.egress_profile_id
        WHERE mc.id = ?
      `).get(params.id) as Record<string, unknown> | null;
      if (!connection) return fail(set, 404, "CONNECTION_NOT_FOUND", "Meta connection not found");
      if (connection.tenant_status !== "active" || connection.status === "disabled" || connection.status === "blocked") {
        return fail(set, 409, "CONNECTION_NOT_PROVISIONABLE", "Meta connection is not eligible for provisioning");
      }
      if (connection.egress_policy !== "direct_allowed" && !connection.egress_profile_id) {
        return fail(set, 409, "EGRESS_ASSIGNMENT_REQUIRED", "Required egress must be assigned before provisioning");
      }
      if (connection.egress_policy !== "direct_allowed" && connection.egress_status !== "healthy") {
        return fail(set, 409, "EGRESS_UNHEALTHY", "Assigned egress must be healthy before provisioning");
      }

      const now = new Date();
      const expiresAt = new Date(now.getTime() + ttlSeconds * 1000).toISOString();
      const claimId = crypto.randomUUID();
      const rawClaim = issueRawClaim(claimId);
      const secretDigest = digestClaim(rawClaim);
      const apply = db.transaction(() => {
        // A new claim supersedes older unused claims for the same connection. This
        // narrows the usable credential set without deleting audit evidence.
        db.query("UPDATE provisioning_claims SET revoked_at = ? WHERE meta_connection_id = ? AND used_at IS NULL AND revoked_at IS NULL")
          .run(now.toISOString(), params.id);
        db.query(`INSERT INTO provisioning_claims(id, secret_digest, meta_connection_id, tenant_id, matrix_owner_mxid, expires_at, used_at, revoked_at, created_at)
          VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)`)
          .run(claimId, secretDigest, params.id, String(connection.tenant_id), String(connection.matrix_owner_mxid), expiresAt, now.toISOString());
        recordAudit(db, {
          tenantId: String(connection.tenant_id), actorType: "operator", actorId: "authenticated-admin",
          action: "provisioning.claim.issue", entityType: "provisioning_claim", entityId: claimId,
          after: { metaConnectionId: params.id, matrixOwnerMxid: String(connection.matrix_owner_mxid), expiresAt }
        });
      });
      apply();
      set.status = 201;
      return { data: { claim: rawClaim, claimId, metaConnectionId: params.id, expiresAt } };
    })
    .post("/api/v1/meta-connections/:id/provisioning-claims/:claimId/revoke", ({ request, params, set }) => {
      if (!adminAuthorized(request)) return fail(set, 401, "UNAUTHORIZED", "Authentication required");
      const claim = db.query("SELECT * FROM provisioning_claims WHERE id = ? AND meta_connection_id = ?").get(params.claimId, params.id) as Record<string, unknown> | null;
      if (!claim) return fail(set, 404, "PROVISIONING_CLAIM_NOT_FOUND", "Provisioning claim not found");
      if (claim.used_at != null) return fail(set, 409, "PROVISIONING_CLAIM_USED", "Used provisioning claims cannot be revoked");
      if (claim.revoked_at == null) {
        const now = new Date().toISOString();
        const apply = db.transaction(() => {
          db.query("UPDATE provisioning_claims SET revoked_at = ? WHERE id = ? AND used_at IS NULL AND revoked_at IS NULL").run(now, params.claimId);
          recordAudit(db, {
            tenantId: String(claim.tenant_id), actorType: "operator", actorId: "authenticated-admin",
            action: "provisioning.claim.revoke", entityType: "provisioning_claim", entityId: params.claimId,
            before: { revokedAt: null }, after: { revokedAt: now }
          });
        });
        apply();
      }
      return { data: { claimId: params.claimId, revoked: true } };
    })
    .post("/internal/v1/provisioning/consume", ({ request, body, set }) => {
      if (!internalAuthorized(request)) return fail(set, 401, "UNAUTHORIZED", "Authentication required");
      const input = body as { claim?: unknown; metaAccountId?: unknown; matrixOwnerMxid?: unknown };
      if (typeof input?.claim !== "string" || input.claim.length < 40 || input.claim.length > 512 ||
          typeof input.metaAccountId !== "string" || !META_ACCOUNT_RE.test(input.metaAccountId) ||
          typeof input.matrixOwnerMxid !== "string" || !input.matrixOwnerMxid.startsWith("@")) {
        return fail(set, 400, "INVALID_PROVISIONING_REQUEST", "claim, Meta account ID and Matrix owner are required");
      }

      try {
        const digest = digestClaim(input.claim);
        const result = db.transaction(() => {
          const row = readClaim(db, digest);
          if (!row) throw new Error("PROVISIONING_CLAIM_INVALID");
          const now = new Date();
          if (row.revoked_at) throw new Error("PROVISIONING_CLAIM_REVOKED");
          if (row.used_at) throw new Error("PROVISIONING_CLAIM_USED");
          if (Date.parse(row.expires_at) <= now.getTime()) throw new Error("PROVISIONING_CLAIM_EXPIRED");
          if (row.claim_tenant_id !== row.connection_tenant_id) throw new Error("PROVISIONING_IDENTITY_CONFLICT");
          if (row.claim_owner_mxid !== row.connection_owner_mxid || row.connection_owner_mxid !== input.matrixOwnerMxid) throw new Error("PROVISIONING_OWNER_MISMATCH");
          if (row.tenant_status !== "active" || row.connection_status === "disabled" || row.connection_status === "blocked") throw new Error("CONNECTION_NOT_PROVISIONABLE");
          if (row.meta_account_id && row.meta_account_id !== input.metaAccountId) throw new Error("META_ACCOUNT_CONFLICT");
          const other = db.query("SELECT id FROM meta_connections WHERE provider = 'facebook' AND meta_account_id = ? AND id <> ?").get(input.metaAccountId, row.meta_connection_id) as { id: string } | null;
          if (other) throw new Error("META_ACCOUNT_CONFLICT");

          const proxy = row.egress_policy === "direct_allowed" ? null : assignedProxyUrl(row, secrets);
          if (row.egress_policy !== "direct_allowed" && !proxy) throw new Error("EGRESS_ASSIGNMENT_REQUIRED");

          const claimed = db.query("UPDATE provisioning_claims SET used_at = ? WHERE id = ? AND used_at IS NULL AND revoked_at IS NULL").run(now.toISOString(), row.claim_id);
          if (claimed.changes !== 1) throw new Error("PROVISIONING_CLAIM_USED");
          const nextStatus = row.connection_status === "draft" ? "ready" : row.connection_status;
          db.query("UPDATE meta_connections SET meta_account_id = ?, status = ?, updated_at = ? WHERE id = ?")
            .run(input.metaAccountId, nextStatus, now.toISOString(), row.meta_connection_id);
          recordAudit(db, {
            tenantId: row.connection_tenant_id, actorType: "service", actorId: "mautrix-meta",
            action: "provisioning.claim.consume", entityType: "meta_connection", entityId: row.meta_connection_id,
            before: { metaAccountId: row.meta_account_id, status: row.connection_status },
            after: { metaAccountId: input.metaAccountId, status: nextStatus, claimId: row.claim_id }
          });
          return {
            connection_id: row.meta_connection_id,
            tenant_id: row.connection_tenant_id,
            meta_account_id: input.metaAccountId,
            status: nextStatus,
            ...(proxy ? { proxy_url: proxy.proxyUrl, assignment_id: proxy.assignmentId } : {})
          };
        })();
        return result;
      } catch (error) {
        const code = error instanceof Error ? error.message : "PROVISIONING_FAILED";
        const unavailable = new Set(["EGRESS_ASSIGNMENT_REQUIRED", "EGRESS_UNHEALTHY", "EGRESS_SECRET_MISSING", "EGRESS_CONFIGURATION_INVALID"]);
        return fail(set, unavailable.has(code) ? 503 : 409, code, "Provisioning failed closed");
      }
    })
    .post("/internal/v1/provisioning/bind-login", ({ request, body, set }) => {
      if (!internalAuthorized(request)) return fail(set, 401, "UNAUTHORIZED", "Authentication required");
      const input = body as { connectionId?: unknown; metaAccountId?: unknown; loginId?: unknown; matrixOwnerMxid?: unknown };
      if (typeof input?.connectionId !== "string" || !input.connectionId ||
          typeof input.metaAccountId !== "string" || !META_ACCOUNT_RE.test(input.metaAccountId) ||
          typeof input.loginId !== "string" || !LOGIN_ID_RE.test(input.loginId) ||
          typeof input.matrixOwnerMxid !== "string" || !input.matrixOwnerMxid.startsWith("@")) {
        return fail(set, 400, "INVALID_LOGIN_BINDING_REQUEST", "Connection, Meta account, login ID and Matrix owner are required");
      }
      try {
        const result = db.transaction(() => {
          const row = db.query(`SELECT mc.*, t.status AS tenant_status FROM meta_connections mc JOIN tenants t ON t.id = mc.tenant_id WHERE mc.id = ?`).get(input.connectionId) as Record<string, unknown> | null;
          if (!row) throw new Error("CONNECTION_NOT_FOUND");
          if (row.tenant_status !== "active" || row.status === "disabled" || row.status === "blocked") throw new Error("CONNECTION_NOT_PROVISIONABLE");
          if (row.matrix_owner_mxid !== input.matrixOwnerMxid) throw new Error("PROVISIONING_OWNER_MISMATCH");
          if (row.meta_account_id !== input.metaAccountId) throw new Error("META_ACCOUNT_CONFLICT");
          if (row.mautrix_login_id && row.mautrix_login_id !== input.loginId) throw new Error("MAUTRIX_LOGIN_CONFLICT");
          const other = db.query("SELECT id FROM meta_connections WHERE mautrix_login_id = ? AND id <> ?").get(input.loginId, input.connectionId) as { id: string } | null;
          if (other) throw new Error("MAUTRIX_LOGIN_CONFLICT");
          const now = new Date().toISOString();
          db.query("UPDATE meta_connections SET mautrix_login_id = ?, updated_at = ? WHERE id = ?").run(input.loginId, now, input.connectionId);
          recordAudit(db, {
            tenantId: String(row.tenant_id), actorType: "service", actorId: "mautrix-meta",
            action: "provisioning.login.bind", entityType: "meta_connection", entityId: input.connectionId,
            before: { mautrixLoginId: row.mautrix_login_id }, after: { mautrixLoginId: input.loginId }
          });
          return { connection_id: input.connectionId, meta_account_id: input.metaAccountId, login_id: input.loginId, status: String(row.status) };
        })();
        return result;
      } catch (error) {
        const code = error instanceof Error ? error.message : "LOGIN_BINDING_FAILED";
        return fail(set, code === "CONNECTION_NOT_FOUND" ? 404 : 409, code, "Login identity binding failed closed");
      }
    });
}
