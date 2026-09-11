import { timingSafeEqual } from "node:crypto";
import type { Database } from "bun:sqlite";
import { Elysia } from "elysia";
import type { EgressStatus, TrafficClass } from "./domain/models";
import { LATEST_SCHEMA_VERSION, schemaVersion } from "./persistence/migrations";
import { SQLiteAuditRepository, SQLiteEgressProfileRepository, SQLiteMetaConnectionRepository, SQLiteTenantRepository } from "./persistence/sqlite-repositories";
import { EnvironmentSecretProvider, isSupportedSecretRef } from "./security/secrets";
import { EgressResolver, normalizeProxyHost, ResolverError } from "./services/egress-resolver";

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
function unauthorized(set: { status?: number | string }) { set.status = 401; return { error: { code: "UNAUTHORIZED", message: "Authentication required" } }; }
function safeError(set: { status?: number | string }, status: number, code: string, message: string) { set.status = status; return { error: { code, message } }; }
const trafficClasses = new Set<TrafficClass>(["login", "messaging", "media", "e2ee"]);
const egressStatuses = new Set<EgressStatus>(["healthy", "degraded", "disabled"]);

export function createApp(db: Database, adminToken: string, internalToken = "", secretEnv: Record<string, string | undefined> = process.env) {
  const tenants = new SQLiteTenantRepository(db);
  const connections = new SQLiteMetaConnectionRepository(db);
  const egress = new SQLiteEgressProfileRepository(db);
  const audit = new SQLiteAuditRepository(db);
  const resolver = new EgressResolver(connections, egress, new EnvironmentSecretProvider(secretEnv));
  const adminAuthorized = (request: Request) => tokenMatches(bearerToken(request), adminToken, 16);
  const internalAuthorized = (request: Request) => tokenMatches(bearerToken(request), internalToken, 24);

  return new Elysia()
    .get("/health/live", () => ({ status: "ok" }))
    .get("/health/ready", ({ set }) => {
      try {
        db.query("SELECT 1 AS ok").get();
        const version = schemaVersion(db);
        if (version !== LATEST_SCHEMA_VERSION) return safeError(set, 503, "SCHEMA_VERSION_MISMATCH", "Local schema is not current");
        return { status: "ready", schemaVersion: version };
      } catch { return safeError(set, 503, "PERSISTENCE_UNAVAILABLE", "Local persistence is unavailable"); }
    })
    .get("/admin", ({ request, set }) => {
      if (!adminAuthorized(request)) return unauthorized(set);
      set.headers["content-type"] = "text/html; charset=utf-8";
      return "<!doctype html><html><body><h1>Meta Control Plane</h1><p>Administrative surface.</p></body></html>";
    })
    .get("/api/v1/tenants", ({ request, set }) => adminAuthorized(request) ? { data: tenants.list() } : unauthorized(set))
    .post("/api/v1/tenants", ({ request, body, set }) => {
      if (!adminAuthorized(request)) return unauthorized(set);
      const input = body as { slug?: unknown; name?: unknown };
      if (typeof input?.slug !== "string" || typeof input?.name !== "string" || !/^[a-z0-9][a-z0-9-]{1,62}$/.test(input.slug)) return safeError(set, 400, "INVALID_TENANT", "Valid slug and name are required");
      set.status = 201; return { data: tenants.create({ slug: input.slug, name: input.name }) };
    })
    .post("/api/v1/meta-connections", ({ request, body, set }) => {
      if (!adminAuthorized(request)) return unauthorized(set);
      const input = body as { tenantId?: unknown; matrixOwnerMxid?: unknown; metaAccountId?: unknown; mautrixLoginId?: unknown };
      if (typeof input?.tenantId !== "string" || typeof input?.matrixOwnerMxid !== "string" || !input.matrixOwnerMxid.startsWith("@")) return safeError(set, 400, "INVALID_META_CONNECTION", "tenantId and matrixOwnerMxid are required");
      try {
        set.status = 201;
        return { data: connections.create({ tenantId: input.tenantId, matrixOwnerMxid: input.matrixOwnerMxid, ...(typeof input.metaAccountId === "string" ? { metaAccountId: input.metaAccountId } : {}), ...(typeof input.mautrixLoginId === "string" ? { mautrixLoginId: input.mautrixLoginId } : {}) }) };
      } catch { return safeError(set, 409, "TENANT_NOT_ACTIVE", "Tenant is unavailable"); }
    })
    .post("/api/v1/egress-profiles", ({ request, body, set }) => {
      if (!adminAuthorized(request)) return unauthorized(set);
      const input = body as Record<string, unknown>;
      if (typeof input.provider !== "string" || typeof input.scheme !== "string" || typeof input.host !== "string" || typeof input.port !== "number" || !Number.isInteger(input.port) || input.port < 1 || input.port > 65535) return safeError(set, 400, "INVALID_EGRESS_PROFILE", "provider, scheme, host and valid port are required");
      const normalizedHost = normalizeProxyHost(input.host);
      if (!normalizedHost) return safeError(set, 400, "INVALID_EGRESS_HOST", "Proxy host must be a valid DNS name or IP address");
      if (input.secretRef != null && (typeof input.secretRef !== "string" || !isSupportedSecretRef(input.secretRef))) return safeError(set, 400, "INVALID_SECRET_REF", "Only env:VARIABLE secret references are supported");
      if (input.status != null && (typeof input.status !== "string" || !egressStatuses.has(input.status as EgressStatus))) return safeError(set, 400, "INVALID_EGRESS_STATUS", "Unsupported egress status");
      const status: EgressStatus = (input.status as EgressStatus | undefined) ?? "healthy";
      const persistedHost = normalizedHost.startsWith("[") ? normalizedHost.slice(1, -1) : normalizedHost;
      const profile = egress.create({ provider: input.provider, scheme: input.scheme, host: persistedHost, port: input.port, username: typeof input.username === "string" ? input.username : null, secretRef: typeof input.secretRef === "string" ? input.secretRef : null, country: typeof input.country === "string" ? input.country : null, region: typeof input.region === "string" ? input.region : null, stickySessionId: typeof input.stickySessionId === "string" ? input.stickySessionId : null, expectedExitIp: typeof input.expectedExitIp === "string" ? input.expectedExitIp : null, status });
      set.status = 201;
      return { data: { ...profile, secretRef: profile.secretRef ? "[configured]" : null } };
    })
    .post("/api/v1/meta-connections/:id/egress", ({ request, params, body, set }) => {
      if (!adminAuthorized(request)) return unauthorized(set);
      const input = body as { egressProfileId?: unknown };
      if (typeof input?.egressProfileId !== "string") return safeError(set, 400, "INVALID_EGRESS_ASSIGNMENT", "egressProfileId is required");
      const before = connections.findById(params.id);
      if (!before) return safeError(set, 404, "CONNECTION_NOT_FOUND", "Meta connection not found");
      try {
        const apply = db.transaction(() => {
          const after = connections.assignEgress(params.id, input.egressProfileId as string);
          audit.record({ tenantId: after.tenantId, actorType: "operator", actorId: "authenticated-admin", action: "egress.assign", entityType: "meta_connection", entityId: after.id, beforeJson: JSON.stringify({ egressProfileId: before.egressProfileId }), afterJson: JSON.stringify({ egressProfileId: after.egressProfileId }) });
          return after;
        });
        return { data: apply() };
      } catch { return safeError(set, 404, "EGRESS_NOT_FOUND", "Egress profile not found"); }
    })
    .post("/api/v1/meta-connections/:id/activate", ({ request, params, set }) => {
      if (!adminAuthorized(request)) return unauthorized(set);
      const connection = connections.findById(params.id);
      if (!connection) return safeError(set, 404, "CONNECTION_NOT_FOUND", "Meta connection not found");
      if (connection.egressPolicy === "proxy_required" && !connection.egressProfileId) return safeError(set, 409, "EGRESS_ASSIGNMENT_REQUIRED", "Required egress must be assigned before activation");
      if (!connection.metaAccountId && !connection.mautrixLoginId) return safeError(set, 409, "PROVIDER_IDENTITY_REQUIRED", "Provider identity must be bound before activation");
      const activate = db.transaction(() => {
        const after = connections.setStatus(params.id, "active");
        audit.record({ tenantId: after.tenantId, actorType: "operator", actorId: "authenticated-admin", action: "connection.activate", entityType: "meta_connection", entityId: after.id, beforeJson: JSON.stringify({ status: connection.status }), afterJson: JSON.stringify({ status: after.status }) });
        return after;
      });
      return { data: activate() };
    })
    .get("/internal/v1/egress/resolve", ({ request, query, set }) => {
      if (!internalAuthorized(request)) return unauthorized(set);
      const metaAccountId = typeof query.meta_account_id === "string" && query.meta_account_id ? query.meta_account_id : undefined;
      const loginId = typeof query.login_id === "string" && query.login_id ? query.login_id : undefined;
      const reason = typeof query.reason === "string" ? query.reason : "";
      const trafficClass = query.traffic_class as TrafficClass;
      if ((!metaAccountId && !loginId) || !reason || !trafficClasses.has(trafficClass)) return safeError(set, 400, "INVALID_RESOLVER_REQUEST", "Stable identity, reason and traffic_class are required");
      try {
        const result = resolver.resolve({ ...(metaAccountId ? { metaAccountId } : {}), ...(loginId ? { loginId } : {}), reason, trafficClass });
        return { proxy_url: result.proxyUrl, assignment_id: result.assignmentId };
      } catch (error) {
        const code = error instanceof ResolverError ? error.code : "EGRESS_RESOLUTION_FAILED";
        const status = code === "IDENTITY_CONFLICT" ? 409 : 503;
        return safeError(set, status, code, "Egress resolution failed closed");
      }
    });
}
