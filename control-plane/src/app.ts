import type { Database } from "bun:sqlite";
import { Elysia } from "elysia";
import { LATEST_SCHEMA_VERSION, schemaVersion } from "./persistence/migrations";
import { SQLiteMetaConnectionRepository, SQLiteTenantRepository } from "./persistence/sqlite-repositories";

function bearerToken(request: Request): string | null {
  const value = request.headers.get("authorization");
  if (!value?.startsWith("Bearer ")) return null;
  return value.slice(7);
}

function unauthorized(set: { status?: number | string }): { error: { code: string; message: string } } {
  set.status = 401;
  return { error: { code: "UNAUTHORIZED", message: "Authentication required" } };
}

export function createApp(db: Database, adminToken: string) {
  const tenants = new SQLiteTenantRepository(db);
  const connections = new SQLiteMetaConnectionRepository(db);
  const authorized = (request: Request) => adminToken.length >= 16 && bearerToken(request) === adminToken;

  return new Elysia()
    .get("/health/live", () => ({ status: "ok" }))
    .get("/health/ready", ({ set }) => {
      try {
        db.query("SELECT 1 AS ok").get();
        const version = schemaVersion(db);
        if (version !== LATEST_SCHEMA_VERSION) {
          set.status = 503;
          return { status: "not_ready", reason: "schema_version", schemaVersion: version, expectedSchemaVersion: LATEST_SCHEMA_VERSION };
        }
        return { status: "ready", schemaVersion: version };
      } catch {
        set.status = 503;
        return { status: "not_ready", reason: "persistence" };
      }
    })
    .get("/admin", ({ request, set }) => {
      if (!authorized(request)) return unauthorized(set);
      set.headers["content-type"] = "text/html; charset=utf-8";
      return "<!doctype html><html><body><h1>Meta Control Plane</h1><p>Phase 1 administrative surface.</p></body></html>";
    })
    .get("/api/v1/tenants", ({ request, set }) => authorized(request) ? { data: tenants.list() } : unauthorized(set))
    .post("/api/v1/tenants", ({ request, body, set }) => {
      if (!authorized(request)) return unauthorized(set);
      const input = body as { slug?: unknown; name?: unknown };
      if (typeof input?.slug !== "string" || typeof input?.name !== "string" || !/^[a-z0-9][a-z0-9-]{1,62}$/.test(input.slug)) {
        set.status = 400;
        return { error: { code: "INVALID_TENANT", message: "Valid slug and name are required" } };
      }
      set.status = 201;
      return { data: tenants.create({ slug: input.slug, name: input.name }) };
    })
    .post("/api/v1/meta-connections", ({ request, body, set }) => {
      if (!authorized(request)) return unauthorized(set);
      const input = body as { tenantId?: unknown; matrixOwnerMxid?: unknown };
      if (typeof input?.tenantId !== "string" || typeof input?.matrixOwnerMxid !== "string" || !input.matrixOwnerMxid.startsWith("@")) {
        set.status = 400;
        return { error: { code: "INVALID_META_CONNECTION", message: "tenantId and matrixOwnerMxid are required" } };
      }
      try {
        set.status = 201;
        return { data: connections.create({ tenantId: input.tenantId, matrixOwnerMxid: input.matrixOwnerMxid }) };
      } catch {
        set.status = 409;
        return { error: { code: "TENANT_NOT_ACTIVE", message: "Tenant is unavailable" } };
      }
    });
}
