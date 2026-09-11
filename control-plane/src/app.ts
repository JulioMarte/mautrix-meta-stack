import { timingSafeEqual } from "node:crypto";
import type { Database } from "bun:sqlite";
import { Elysia } from "elysia";
import type { Attachment, ChatwootBindingStatus, EgressStatus, MatrixEncryptedFile, TrafficClass } from "./domain/models";
import { LATEST_SCHEMA_VERSION, schemaVersion } from "./persistence/migrations";
import {
  SQLiteAuditRepository, SQLiteChatwootBindingRepository, SQLiteConversationBindingRepository,
  SQLiteEgressProfileRepository, SQLiteMetaConnectionRepository, SQLiteProcessedEventRepository, SQLiteTenantRepository
} from "./persistence/sqlite-repositories";
import { ChatwootEnvironmentSecretProvider, EnvironmentSecretProvider, isSupportedChatwootSecretRef, isSupportedSecretRef } from "./security/secrets";
import type { ChatwootGateway } from "./services/chatwoot-gateway";
import { EgressResolver, normalizeProxyHost, normalizeProxyScheme, ResolverError } from "./services/egress-resolver";
import { HttpChatwootGateway } from "./services/http-chatwoot-gateway";
import { HttpMatrixMediaDownloader } from "./services/matrix-media-downloader";
import { MatrixToChatwootService, type MatrixInboundEvent } from "./services/matrix-to-chatwoot";

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
const chatwootStatuses = new Set<ChatwootBindingStatus>(["active", "disabled"]);
const attachmentKinds = new Set<Attachment["kind"]>(["image", "video", "audio", "file", "unknown"]);

function validChatwootBaseUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return (url.protocol === "http:" || url.protocol === "https:") && !url.username && !url.password && !url.search && !url.hash;
  } catch { return false; }
}

function parseEncryptedFile(value: unknown): MatrixEncryptedFile | null {
  if (!value || typeof value !== "object") return null;
  const input = value as Record<string, unknown>;
  if (input.v !== "v2" || typeof input.iv !== "string" || !input.iv) return null;
  if (!input.key || typeof input.key !== "object" || !input.hashes || typeof input.hashes !== "object") return null;
  const key = input.key as Record<string, unknown>;
  const hashes = input.hashes as Record<string, unknown>;
  if (key.kty !== "oct" || key.alg !== "A256CTR" || typeof key.k !== "string" || !key.k || key.ext !== true) return null;
  if (!Array.isArray(key.keyOps) || !key.keyOps.every((op) => typeof op === "string") || !key.keyOps.includes("decrypt")) return null;
  if (typeof hashes.sha256 !== "string" || !hashes.sha256) return null;
  return {
    v: "v2",
    key: { kty: "oct", alg: "A256CTR", k: key.k, keyOps: key.keyOps as string[], ext: true },
    iv: input.iv,
    hashes: { sha256: hashes.sha256 }
  };
}

function parseMatrixEvent(body: unknown): MatrixInboundEvent | null {
  if (!body || typeof body !== "object") return null;
  const input = body as Record<string, unknown>;
  for (const key of ["connectionId", "roomId", "remoteThreadId", "remoteContactId", "eventId", "senderId", "occurredAt"]) {
    if (typeof input[key] !== "string" || !(input[key] as string).length) return null;
  }
  if (input.provenance !== "meta" && input.provenance !== "chatwoot") return null;
  if (input.senderDisplayName != null && typeof input.senderDisplayName !== "string") return null;
  if (input.text != null && typeof input.text !== "string") return null;
  if (!Number.isFinite(Date.parse(input.occurredAt as string))) return null;
  const rawAttachments = input.attachments ?? [];
  if (!Array.isArray(rawAttachments)) return null;
  const attachments: Attachment[] = [];
  for (const raw of rawAttachments) {
    if (!raw || typeof raw !== "object") return null;
    const a = raw as Record<string, unknown>;
    if (typeof a.kind !== "string" || !attachmentKinds.has(a.kind as Attachment["kind"])) return null;
    if (a.id != null && typeof a.id !== "string") return null;
    if (a.url != null && typeof a.url !== "string") return null;
    if (a.mimeType != null && typeof a.mimeType !== "string") return null;
    if (a.fileName != null && typeof a.fileName !== "string") return null;
    if (a.sizeBytes != null && (typeof a.sizeBytes !== "number" || !Number.isSafeInteger(a.sizeBytes) || a.sizeBytes < 0)) return null;
    if (a.voiceNote != null && typeof a.voiceNote !== "boolean") return null;
    if (a.voiceNote === true && a.kind !== "audio") return null;
    const encryption = a.encryption == null ? undefined : parseEncryptedFile(a.encryption);
    if (a.encryption != null && !encryption) return null;
    attachments.push({
      kind: a.kind as Attachment["kind"],
      ...(typeof a.id === "string" ? { id: a.id } : {}),
      ...(typeof a.url === "string" ? { url: a.url } : {}),
      ...(typeof a.mimeType === "string" ? { mimeType: a.mimeType } : {}),
      ...(typeof a.fileName === "string" ? { fileName: a.fileName } : {}),
      ...(typeof a.sizeBytes === "number" ? { sizeBytes: a.sizeBytes } : {}),
      ...(a.voiceNote === true ? { voiceNote: true } : {}),
      ...(encryption ? { encryption } : {})
    });
  }
  return {
    connectionId: input.connectionId as string,
    roomId: input.roomId as string,
    remoteThreadId: input.remoteThreadId as string,
    remoteContactId: input.remoteContactId as string,
    eventId: input.eventId as string,
    senderId: input.senderId as string,
    ...(typeof input.senderDisplayName === "string" ? { senderDisplayName: input.senderDisplayName } : {}),
    ...(typeof input.text === "string" ? { text: input.text } : {}),
    ...(attachments.length ? { attachments } : {}),
    occurredAt: input.occurredAt as string,
    provenance: input.provenance
  };
}

export function createApp(
  db: Database,
  adminToken: string,
  internalToken = "",
  secretEnv: Record<string, string | undefined> = process.env,
  dependencies: { chatwootGateway?: ChatwootGateway } = {}
) {
  const tenants = new SQLiteTenantRepository(db);
  const connections = new SQLiteMetaConnectionRepository(db);
  const egress = new SQLiteEgressProfileRepository(db);
  const chatwootBindings = new SQLiteChatwootBindingRepository(db);
  const conversationBindings = new SQLiteConversationBindingRepository(db);
  const processedEvents = new SQLiteProcessedEventRepository(db);
  const audit = new SQLiteAuditRepository(db);
  const resolver = new EgressResolver(connections, egress, new EnvironmentSecretProvider(secretEnv));
  const chatwootGateway = dependencies.chatwootGateway ?? new HttpChatwootGateway(new ChatwootEnvironmentSecretProvider(secretEnv), fetch, 8_000, new HttpMatrixMediaDownloader(secretEnv));
  const matrixToChatwoot = new MatrixToChatwootService(tenants, connections, chatwootBindings, conversationBindings, processedEvents, chatwootGateway);
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
      const normalizedScheme = normalizeProxyScheme(input.scheme);
      if (!normalizedScheme) return safeError(set, 400, "INVALID_EGRESS_SCHEME", "Proxy scheme must be http, https or socks5");
      const normalizedHost = normalizeProxyHost(input.host);
      if (!normalizedHost) return safeError(set, 400, "INVALID_EGRESS_HOST", "Proxy host must be a valid DNS name or IP address");
      if (input.secretRef != null && (typeof input.secretRef !== "string" || !isSupportedSecretRef(input.secretRef))) return safeError(set, 400, "INVALID_SECRET_REF", "Only env:EGRESS_PROXY_* secret references are supported");
      const username = typeof input.username === "string" && input.username.length > 0 ? input.username : null;
      const secretRef = typeof input.secretRef === "string" ? input.secretRef : null;
      if ((username === null) !== (secretRef === null)) return safeError(set, 400, "INVALID_PROXY_AUTH", "Proxy username and secretRef must be configured together");
      if (input.status != null && (typeof input.status !== "string" || !egressStatuses.has(input.status as EgressStatus))) return safeError(set, 400, "INVALID_EGRESS_STATUS", "Unsupported egress status");
      const status: EgressStatus = (input.status as EgressStatus | undefined) ?? "healthy";
      const persistedHost = normalizedHost.startsWith("[") ? normalizedHost.slice(1, -1) : normalizedHost;
      const profile = egress.create({ provider: input.provider, scheme: normalizedScheme, host: persistedHost, port: input.port, username, secretRef, country: typeof input.country === "string" ? input.country : null, region: typeof input.region === "string" ? input.region : null, stickySessionId: typeof input.stickySessionId === "string" ? input.stickySessionId : null, expectedExitIp: typeof input.expectedExitIp === "string" ? input.expectedExitIp : null, status });
      set.status = 201;
      return { data: { ...profile, secretRef: profile.secretRef ? "[configured]" : null } };
    })
    .post("/api/v1/chatwoot-bindings", ({ request, body, set }) => {
      if (!adminAuthorized(request)) return unauthorized(set);
      const input = body as Record<string, unknown>;
      if (typeof input.tenantId !== "string" || typeof input.chatwootAccountId !== "string" || typeof input.chatwootInboxId !== "string" || typeof input.apiBaseUrl !== "string" || typeof input.credentialRef !== "string") return safeError(set, 400, "INVALID_CHATWOOT_BINDING", "tenantId, account, inbox, base URL and credential reference are required");
      if (!/^[1-9][0-9]*$/.test(input.chatwootAccountId) || !/^[1-9][0-9]*$/.test(input.chatwootInboxId)) return safeError(set, 400, "INVALID_CHATWOOT_IDS", "Chatwoot account and inbox IDs must be positive numeric identifiers");
      if (!validChatwootBaseUrl(input.apiBaseUrl)) return safeError(set, 400, "INVALID_CHATWOOT_BASE_URL", "Chatwoot base URL must be http(s) without credentials, query or fragment");
      if (!isSupportedChatwootSecretRef(input.credentialRef)) return safeError(set, 400, "INVALID_CHATWOOT_SECRET_REF", "Only env:CHATWOOT_* secret references are supported");
      if (input.status != null && (typeof input.status !== "string" || !chatwootStatuses.has(input.status as ChatwootBindingStatus))) return safeError(set, 400, "INVALID_CHATWOOT_STATUS", "Unsupported Chatwoot binding status");
      try {
        const binding = chatwootBindings.create({
          tenantId: input.tenantId,
          chatwootAccountId: input.chatwootAccountId,
          chatwootInboxId: input.chatwootInboxId,
          apiBaseUrl: input.apiBaseUrl.replace(/\/+$/, ""),
          credentialRef: input.credentialRef,
          status: (input.status as ChatwootBindingStatus | undefined) ?? "active"
        });
        set.status = 201;
        return { data: { ...binding, credentialRef: "[configured]" } };
      } catch { return safeError(set, 409, "CHATWOOT_BINDING_CREATE_FAILED", "Chatwoot binding could not be created"); }
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
    .post("/api/v1/meta-connections/:id/chatwoot", ({ request, params, body, set }) => {
      if (!adminAuthorized(request)) return unauthorized(set);
      const input = body as { chatwootBindingId?: unknown };
      if (typeof input?.chatwootBindingId !== "string") return safeError(set, 400, "INVALID_CHATWOOT_ASSIGNMENT", "chatwootBindingId is required");
      const before = connections.findById(params.id);
      if (!before) return safeError(set, 404, "CONNECTION_NOT_FOUND", "Meta connection not found");
      try {
        const apply = db.transaction(() => {
          const after = connections.assignChatwootBinding(params.id, input.chatwootBindingId as string);
          audit.record({ tenantId: after.tenantId, actorType: "operator", actorId: "authenticated-admin", action: "chatwoot.assign", entityType: "meta_connection", entityId: after.id, beforeJson: JSON.stringify({ chatwootBindingId: before.chatwootBindingId }), afterJson: JSON.stringify({ chatwootBindingId: after.chatwootBindingId }) });
          return after;
        });
        return { data: apply() };
      } catch (error) {
        const code = error instanceof Error ? error.message : "CHATWOOT_ASSIGNMENT_FAILED";
        return safeError(set, code === "CROSS_TENANT_CHATWOOT_BINDING" ? 409 : 404, code, "Chatwoot binding assignment failed");
      }
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
    })
    .post("/internal/v1/matrix/events", async ({ request, body, set }) => {
      if (!internalAuthorized(request)) return unauthorized(set);
      const event = parseMatrixEvent(body);
      if (!event) return safeError(set, 400, "INVALID_MATRIX_EVENT", "Matrix event payload is invalid");
      try {
        const result = await matrixToChatwoot.handle(event);
        return { data: result };
      } catch (error) {
        const code = error instanceof Error ? error.message : "MATRIX_TO_CHATWOOT_FAILED";
        const terminalCodes = new Set(["CONNECTION_NOT_FOUND", "TENANT_NOT_ACTIVE", "CONNECTION_NOT_ACTIVE", "CHATWOOT_BINDING_REQUIRED", "CHATWOOT_BINDING_NOT_ACTIVE", "CROSS_TENANT_CHATWOOT_BINDING", "EVENT_IDENTITY_CONFLICT", "MATRIX_ROOM_BINDING_CONFLICT", "EMPTY_MESSAGE", "CHATWOOT_ATTACHMENT_LIMIT_EXCEEDED", "MATRIX_MEDIA_ENCRYPTION_INVALID", "MATRIX_MEDIA_HASH_MISMATCH"]);
        return safeError(set, terminalCodes.has(code) ? 409 : 503, code, "Matrix to Chatwoot delivery failed");
      }
    });
}
