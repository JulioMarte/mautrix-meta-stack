import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import type { Database } from "bun:sqlite";
import { Elysia } from "elysia";
import {
  SQLiteAuditRepository,
  SQLiteChatwootBindingRepository,
  SQLiteEgressProfileRepository,
  SQLiteMetaConnectionRepository,
  SQLiteTenantRepository
} from "./persistence/sqlite-repositories";
import { isSupportedChatwootSecretRef, isSupportedSecretRef } from "./security/secrets";
import { normalizeProxyHost, normalizeProxyScheme } from "./services/egress-resolver";

const SESSION_COOKIE = "mcp_admin_session";
const SESSION_TTL_SECONDS = 8 * 60 * 60;

type MutableSet = {
  status?: number | string;
  headers: Record<string, string>;
};

function constantTimeEqual(leftValue: string, rightValue: string): boolean {
  const left = Buffer.from(leftValue);
  const right = Buffer.from(rightValue);
  return left.length === right.length && timingSafeEqual(left, right);
}

function sign(adminToken: string, value: string): string {
  return createHmac("sha256", adminToken).update(value).digest("base64url");
}

function issueSession(adminToken: string, now = Date.now()): string {
  const issuedAt = Math.floor(now / 1000);
  const payload = `${issuedAt}.${randomBytes(18).toString("base64url")}`;
  return `${payload}.${sign(adminToken, `session:${payload}`)}`;
}

function parseCookies(request: Request): Map<string, string> {
  const values = new Map<string, string>();
  for (const part of (request.headers.get("cookie") ?? "").split(";")) {
    const separator = part.indexOf("=");
    if (separator <= 0) continue;
    const key = part.slice(0, separator).trim();
    const value = part.slice(separator + 1).trim();
    if (key) values.set(key, value);
  }
  return values;
}

function validSession(request: Request, adminToken: string, now = Date.now()): string | null {
  const session = parseCookies(request).get(SESSION_COOKIE);
  if (!session) return null;
  const parts = session.split(".");
  if (parts.length !== 3) return null;
  const [issuedAtRaw, nonce, signature] = parts;
  if (!issuedAtRaw || !nonce || !signature || !/^\d+$/.test(issuedAtRaw)) return null;
  const issuedAt = Number(issuedAtRaw);
  const nowSeconds = Math.floor(now / 1000);
  if (!Number.isSafeInteger(issuedAt) || issuedAt > nowSeconds + 60 || nowSeconds - issuedAt > SESSION_TTL_SECONDS) return null;
  const payload = `${issuedAtRaw}.${nonce}`;
  const expected = sign(adminToken, `session:${payload}`);
  return constantTimeEqual(signature, expected) ? session : null;
}

function csrfToken(adminToken: string, session: string): string {
  return sign(adminToken, `csrf:${session}`);
}

function bearerToken(request: Request): string | null {
  const value = request.headers.get("authorization");
  return value?.startsWith("Bearer ") ? value.slice(7) : null;
}

function adminTokenMatches(candidate: string | null, adminToken: string): boolean {
  return !!candidate && adminToken.length >= 16 && constantTimeEqual(candidate, adminToken);
}

function formValue(body: unknown, key: string): string {
  if (!body || typeof body !== "object") return "";
  const value = (body as Record<string, unknown>)[key];
  return typeof value === "string" ? value.trim() : "";
}

function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function html(set: MutableSet, content: string, status = 200): string {
  set.status = status;
  set.headers["content-type"] = "text/html; charset=utf-8";
  set.headers["cache-control"] = "no-store";
  set.headers["referrer-policy"] = "no-referrer";
  set.headers["x-content-type-options"] = "nosniff";
  set.headers["content-security-policy"] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'";
  return content;
}

function redirect(set: MutableSet, location: string): string {
  set.status = 303;
  set.headers.location = location;
  set.headers["cache-control"] = "no-store";
  return "";
}

function page(title: string, body: string): string {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${escapeHtml(title)}</title><style>body{font-family:system-ui,sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem;line-height:1.45}table{border-collapse:collapse;width:100%;margin:1rem 0 2rem}th,td{border:1px solid #ccc;padding:.45rem;text-align:left;vertical-align:top}form{margin:.5rem 0}fieldset{margin:1rem 0;padding:1rem}label{display:block;margin:.5rem 0}input,select,button{font:inherit;padding:.35rem}code{word-break:break-all}.warn{font-weight:700}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem}</style></head><body>${body}</body></html>`;
}

function loginPage(message = ""): string {
  return page("Meta Control Plane Login", `<h1>Meta Control Plane</h1>${message ? `<p class="warn">${escapeHtml(message)}</p>` : ""}<p>Enter the configured control-plane admin token. The token is exchanged for a short-lived signed HttpOnly session cookie and is never placed in a URL.</p><form method="post" action="/admin/login"><label>Admin token <input name="token" type="password" autocomplete="current-password" required></label><button type="submit">Sign in</button></form>`);
}

function requireSession(request: Request, set: MutableSet, adminToken: string): string | null {
  const session = validSession(request, adminToken);
  if (session) return session;
  set.status = 401;
  return null;
}

function requireMutationSession(request: Request, body: unknown, set: MutableSet, adminToken: string): string | null {
  const session = requireSession(request, set, adminToken);
  if (!session) return null;
  const supplied = formValue(body, "csrf");
  const expected = csrfToken(adminToken, session);
  if (!supplied || !constantTimeEqual(supplied, expected)) {
    set.status = 403;
    return null;
  }
  return session;
}

function errorPage(set: MutableSet, status: number, message: string): string {
  return html(set, page("Admin operation failed", `<h1>Admin operation failed</h1><p>${escapeHtml(message)}</p><p><a href="/admin/dashboard">Return to dashboard</a></p>`), status);
}

function dashboard(db: Database, csrf: string): string {
  const tenants = db.query("SELECT id, slug, name, status FROM tenants ORDER BY created_at, id").all() as Array<Record<string, unknown>>;
  const connections = db.query("SELECT id, tenant_id, matrix_owner_mxid, meta_account_id, mautrix_login_id, egress_profile_id, chatwoot_binding_id, egress_policy, status FROM meta_connections ORDER BY created_at, id").all() as Array<Record<string, unknown>>;
  const egress = db.query("SELECT id, provider, scheme, host, port, username, country, region, expected_exit_ip, last_verified_exit_ip, status, last_checked_at, failure_count, secret_ref IS NOT NULL AS has_secret FROM egress_profiles ORDER BY created_at, id").all() as Array<Record<string, unknown>>;
  const chatwoot = db.query("SELECT id, tenant_id, chatwoot_account_id, chatwoot_inbox_id, api_base_url, status FROM chatwoot_bindings ORDER BY created_at, id").all() as Array<Record<string, unknown>>;
  const audits = db.query("SELECT created_at, tenant_id, action, entity_type, entity_id FROM audit_events ORDER BY created_at DESC, id DESC LIMIT 50").all() as Array<Record<string, unknown>>;
  const events = db.query("SELECT source, source_event_id, meta_connection_id, status, first_seen_at, processed_at FROM processed_events ORDER BY first_seen_at DESC, id DESC LIMIT 50").all() as Array<Record<string, unknown>>;
  const hidden = `<input type="hidden" name="csrf" value="${escapeHtml(csrf)}">`;
  const tenantOptions = tenants.filter((t) => t.status === "active").map((t) => `<option value="${escapeHtml(t.id)}">${escapeHtml(t.slug)} (${escapeHtml(t.id)})</option>`).join("");
  const egressOptions = egress.filter((p) => p.status !== "disabled").map((p) => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.provider)} ${escapeHtml(p.host)}:${escapeHtml(p.port)} (${escapeHtml(p.status)})</option>`).join("");

  const tenantRows = tenants.map((t) => `<tr><td>${escapeHtml(t.slug)}</td><td>${escapeHtml(t.name)}</td><td>${escapeHtml(t.status)}</td><td><code>${escapeHtml(t.id)}</code></td><td>${t.status === "active" ? `<form method="post" action="/admin/tenants/${encodeURIComponent(String(t.id))}/disable">${hidden}<button type="submit">Disable</button></form>` : ""}</td></tr>`).join("");
  const connectionRows = connections.map((c) => {
    const tenantBindings = chatwoot.filter((b) => b.tenant_id === c.tenant_id && b.status === "active").map((b) => `<option value="${escapeHtml(b.id)}">acct ${escapeHtml(b.chatwoot_account_id)} / inbox ${escapeHtml(b.chatwoot_inbox_id)}</option>`).join("");
    return `<tr><td><code>${escapeHtml(c.id)}</code></td><td><code>${escapeHtml(c.tenant_id)}</code></td><td>${escapeHtml(c.matrix_owner_mxid)}</td><td>${escapeHtml(c.meta_account_id || "unbound")}</td><td>${escapeHtml(c.mautrix_login_id || "unbound")}</td><td>${escapeHtml(c.status)}</td><td>${escapeHtml(c.egress_policy)}</td><td><form method="post" action="/admin/meta-connections/${encodeURIComponent(String(c.id))}/egress">${hidden}<select name="egressProfileId" required><option value="">Select egress</option>${egressOptions}</select><button type="submit">Assign</button></form><form method="post" action="/admin/meta-connections/${encodeURIComponent(String(c.id))}/chatwoot">${hidden}<select name="chatwootBindingId" required><option value="">Select Chatwoot</option>${tenantBindings}</select><button type="submit">Bind</button></form>${c.status !== "disabled" ? `<form method="post" action="/admin/meta-connections/${encodeURIComponent(String(c.id))}/disable">${hidden}<button type="submit">Disable now</button></form>` : ""}${c.status !== "active" && c.status !== "disabled" ? `<form method="post" action="/admin/meta-connections/${encodeURIComponent(String(c.id))}/activate">${hidden}<button type="submit">Activate</button></form>` : ""}</td></tr>`;
  }).join("");
  const egressRows = egress.map((p) => `<tr><td><code>${escapeHtml(p.id)}</code></td><td>${escapeHtml(p.provider)}</td><td>${escapeHtml(p.scheme)}://${escapeHtml(p.host)}:${escapeHtml(p.port)}</td><td>${escapeHtml(p.country || "")}${p.region ? ` / ${escapeHtml(p.region)}` : ""}</td><td>${escapeHtml(p.expected_exit_ip || "")}</td><td>${escapeHtml(p.last_verified_exit_ip || "")}</td><td>${escapeHtml(p.status)}</td><td>${escapeHtml(p.failure_count)}</td><td>${escapeHtml(p.last_checked_at || "never")}</td><td>${Number(p.has_secret) ? "configured" : "none"}</td></tr>`).join("");
  const chatwootRows = chatwoot.map((b) => `<tr><td><code>${escapeHtml(b.id)}</code></td><td><code>${escapeHtml(b.tenant_id)}</code></td><td>${escapeHtml(b.chatwoot_account_id)}</td><td>${escapeHtml(b.chatwoot_inbox_id)}</td><td>${escapeHtml(b.api_base_url)}</td><td>${escapeHtml(b.status)}</td></tr>`).join("");
  const auditRows = audits.map((a) => `<tr><td>${escapeHtml(a.created_at)}</td><td>${escapeHtml(a.action)}</td><td>${escapeHtml(a.entity_type)}</td><td><code>${escapeHtml(a.entity_id)}</code></td><td><code>${escapeHtml(a.tenant_id || "")}</code></td></tr>`).join("");
  const eventRows = events.map((e) => `<tr><td>${escapeHtml(e.first_seen_at)}</td><td>${escapeHtml(e.source)}</td><td><code>${escapeHtml(e.source_event_id)}</code></td><td><code>${escapeHtml(e.meta_connection_id || "")}</code></td><td>${escapeHtml(e.status)}</td><td>${escapeHtml(e.processed_at || "pending")}</td></tr>`).join("");

  return page("Meta Control Plane Admin", `<h1>Meta Control Plane</h1><p class="warn">Administrative actions affect live routing. Raw proxy passwords, Chatwoot tokens and Meta session material are intentionally not rendered here.</p><form method="post" action="/admin/logout">${hidden}<button type="submit">Sign out</button></form><div class="grid"><fieldset><legend>Create tenant</legend><form method="post" action="/admin/tenants">${hidden}<label>Slug <input name="slug" pattern="[a-z0-9][a-z0-9-]{1,62}" required></label><label>Name <input name="name" required></label><button type="submit">Create tenant</button></form></fieldset><fieldset><legend>Create Meta connection</legend><form method="post" action="/admin/meta-connections">${hidden}<label>Tenant <select name="tenantId" required>${tenantOptions}</select></label><label>Matrix owner MXID <input name="matrixOwnerMxid" placeholder="@owner:example.org" required></label><button type="submit">Create connection</button></form></fieldset><fieldset><legend>Create egress profile</legend><form method="post" action="/admin/egress-profiles">${hidden}<label>Provider <input name="provider" required></label><label>Scheme <select name="scheme"><option>http</option><option>https</option><option>socks5</option></select></label><label>Host <input name="host" required></label><label>Port <input name="port" inputmode="numeric" required></label><label>Username (optional) <input name="username"></label><label>Secret reference (optional, env:EGRESS_PROXY_*) <input name="secretRef"></label><label>Country <input name="country"></label><label>Region <input name="region"></label><label>Expected exit IP <input name="expectedExitIp"></label><button type="submit">Create egress</button></form></fieldset><fieldset><legend>Create Chatwoot binding</legend><form method="post" action="/admin/chatwoot-bindings">${hidden}<label>Tenant <select name="tenantId" required>${tenantOptions}</select></label><label>Account ID <input name="chatwootAccountId" inputmode="numeric" required></label><label>Inbox ID <input name="chatwootInboxId" inputmode="numeric" required></label><label>API base URL <input name="apiBaseUrl" type="url" required></label><label>Credential reference (env:CHATWOOT_*) <input name="credentialRef" required></label><button type="submit">Create binding</button></form></fieldset></div><h2>Tenants</h2><table><thead><tr><th>Slug</th><th>Name</th><th>Status</th><th>ID</th><th>Action</th></tr></thead><tbody>${tenantRows}</tbody></table><h2>Connections</h2><table><thead><tr><th>ID</th><th>Tenant</th><th>Matrix owner</th><th>Meta account</th><th>Mautrix login</th><th>Status</th><th>Egress policy</th><th>Actions</th></tr></thead><tbody>${connectionRows}</tbody></table><h2>Egress health</h2><table><thead><tr><th>ID</th><th>Provider</th><th>Endpoint</th><th>Location</th><th>Expected IP</th><th>Last IP</th><th>Status</th><th>Failures</th><th>Last checked</th><th>Secret</th></tr></thead><tbody>${egressRows}</tbody></table><h2>Chatwoot bindings</h2><table><thead><tr><th>ID</th><th>Tenant</th><th>Account</th><th>Inbox</th><th>Base URL</th><th>Status</th></tr></thead><tbody>${chatwootRows}</tbody></table><h2>Recent routing events</h2><table><thead><tr><th>First seen</th><th>Source</th><th>Event</th><th>Connection</th><th>Status</th><th>Processed</th></tr></thead><tbody>${eventRows}</tbody></table><h2>Recent audit events</h2><table><thead><tr><th>Time</th><th>Action</th><th>Entity</th><th>Entity ID</th><th>Tenant</th></tr></thead><tbody>${auditRows}</tbody></table>`);
}

export function createAdminSurface(db: Database, adminToken: string) {
  const tenants = new SQLiteTenantRepository(db);
  const connections = new SQLiteMetaConnectionRepository(db);
  const egress = new SQLiteEgressProfileRepository(db);
  const chatwootBindings = new SQLiteChatwootBindingRepository(db);
  const audit = new SQLiteAuditRepository(db);

  return new Elysia({ name: "admin-surface" })
    .get("/admin/login", ({ request, set }) => {
      if (validSession(request, adminToken)) return redirect(set as MutableSet, "/admin/dashboard");
      return html(set as MutableSet, loginPage());
    })
    .post("/admin/login", ({ body, set }) => {
      const candidate = formValue(body, "token");
      if (!adminTokenMatches(candidate || null, adminToken)) return html(set as MutableSet, loginPage("Invalid admin token."), 401);
      const session = issueSession(adminToken);
      (set as MutableSet).headers["set-cookie"] = `${SESSION_COOKIE}=${session}; Path=/admin; HttpOnly; Secure; SameSite=Strict; Max-Age=${SESSION_TTL_SECONDS}`;
      return redirect(set as MutableSet, "/admin/dashboard");
    })
    .get("/admin/dashboard", ({ request, set }) => {
      let session = validSession(request, adminToken);
      if (!session) {
        const bearer = bearerToken(request);
        if (!adminTokenMatches(bearer, adminToken)) return html(set as MutableSet, page("Unauthorized", "<h1>Unauthorized</h1><p><a href=\"/admin/login\">Sign in</a></p>"), 401);
        session = issueSession(adminToken);
        (set as MutableSet).headers["set-cookie"] = `${SESSION_COOKIE}=${session}; Path=/admin; HttpOnly; Secure; SameSite=Strict; Max-Age=${SESSION_TTL_SECONDS}`;
      }
      return html(set as MutableSet, dashboard(db, csrfToken(adminToken, session)));
    })
    .post("/admin/logout", ({ request, body, set }) => {
      if (!requireMutationSession(request, body, set as MutableSet, adminToken)) return errorPage(set as MutableSet, Number((set as MutableSet).status) || 403, "Authentication or CSRF validation failed.");
      (set as MutableSet).headers["set-cookie"] = `${SESSION_COOKIE}=; Path=/admin; HttpOnly; Secure; SameSite=Strict; Max-Age=0`;
      return redirect(set as MutableSet, "/admin/login");
    })
    .post("/admin/tenants", ({ request, body, set }) => {
      if (!requireMutationSession(request, body, set as MutableSet, adminToken)) return errorPage(set as MutableSet, Number((set as MutableSet).status) || 403, "Authentication or CSRF validation failed.");
      const slug = formValue(body, "slug");
      const name = formValue(body, "name");
      if (!/^[a-z0-9][a-z0-9-]{1,62}$/.test(slug) || !name) return errorPage(set as MutableSet, 400, "Valid tenant slug and name are required.");
      try {
        const tenant = tenants.create({ slug, name });
        audit.record({ tenantId: tenant.id, actorType: "operator", actorId: "admin-ui", action: "tenant.create", entityType: "tenant", entityId: tenant.id, beforeJson: null, afterJson: JSON.stringify({ status: tenant.status }) });
        return redirect(set as MutableSet, "/admin/dashboard");
      } catch { return errorPage(set as MutableSet, 409, "Tenant could not be created."); }
    })
    .post("/admin/tenants/:id/disable", ({ request, body, params, set }) => {
      if (!requireMutationSession(request, body, set as MutableSet, adminToken)) return errorPage(set as MutableSet, Number((set as MutableSet).status) || 403, "Authentication or CSRF validation failed.");
      const before = tenants.findById(params.id);
      if (!before) return errorPage(set as MutableSet, 404, "Tenant not found.");
      try {
        const apply = db.transaction(() => {
          const after = tenants.setStatus(params.id, "disabled");
          audit.record({ tenantId: after.id, actorType: "operator", actorId: "admin-ui", action: "tenant.disable", entityType: "tenant", entityId: after.id, beforeJson: JSON.stringify({ status: before.status }), afterJson: JSON.stringify({ status: after.status }) });
          return after;
        });
        apply();
        return redirect(set as MutableSet, "/admin/dashboard");
      } catch { return errorPage(set as MutableSet, 409, "Tenant could not be disabled."); }
    })
    .post("/admin/meta-connections", ({ request, body, set }) => {
      if (!requireMutationSession(request, body, set as MutableSet, adminToken)) return errorPage(set as MutableSet, Number((set as MutableSet).status) || 403, "Authentication or CSRF validation failed.");
      const tenantId = formValue(body, "tenantId");
      const matrixOwnerMxid = formValue(body, "matrixOwnerMxid");
      if (!tenantId || !matrixOwnerMxid.startsWith("@") || !matrixOwnerMxid.includes(":")) return errorPage(set as MutableSet, 400, "Tenant and a valid Matrix owner MXID are required.");
      try {
        const connection = connections.create({ tenantId, matrixOwnerMxid });
        audit.record({ tenantId, actorType: "operator", actorId: "admin-ui", action: "connection.create", entityType: "meta_connection", entityId: connection.id, beforeJson: null, afterJson: JSON.stringify({ status: connection.status, egressPolicy: connection.egressPolicy }) });
        return redirect(set as MutableSet, "/admin/dashboard");
      } catch { return errorPage(set as MutableSet, 409, "Meta connection could not be created for that tenant."); }
    })
    .post("/admin/meta-connections/:id/disable", ({ request, body, params, set }) => {
      if (!requireMutationSession(request, body, set as MutableSet, adminToken)) return errorPage(set as MutableSet, Number((set as MutableSet).status) || 403, "Authentication or CSRF validation failed.");
      const before = connections.findById(params.id);
      if (!before) return errorPage(set as MutableSet, 404, "Meta connection not found.");
      try {
        const apply = db.transaction(() => {
          const after = connections.setStatus(params.id, "disabled");
          audit.record({ tenantId: after.tenantId, actorType: "operator", actorId: "admin-ui", action: "connection.disable", entityType: "meta_connection", entityId: after.id, beforeJson: JSON.stringify({ status: before.status }), afterJson: JSON.stringify({ status: after.status }) });
          return after;
        });
        apply();
        return redirect(set as MutableSet, "/admin/dashboard");
      } catch { return errorPage(set as MutableSet, 409, "Connection could not be disabled."); }
    })
    .post("/admin/meta-connections/:id/activate", ({ request, body, params, set }) => {
      if (!requireMutationSession(request, body, set as MutableSet, adminToken)) return errorPage(set as MutableSet, Number((set as MutableSet).status) || 403, "Authentication or CSRF validation failed.");
      const before = connections.findById(params.id);
      if (!before) return errorPage(set as MutableSet, 404, "Meta connection not found.");
      if (before.egressPolicy === "proxy_required" && !before.egressProfileId) return errorPage(set as MutableSet, 409, "Assign required egress before activation.");
      if (!before.metaAccountId && !before.mautrixLoginId) return errorPage(set as MutableSet, 409, "Provider identity must be provisioned before activation.");
      try {
        const apply = db.transaction(() => {
          const after = connections.setStatus(params.id, "active");
          audit.record({ tenantId: after.tenantId, actorType: "operator", actorId: "admin-ui", action: "connection.activate", entityType: "meta_connection", entityId: after.id, beforeJson: JSON.stringify({ status: before.status }), afterJson: JSON.stringify({ status: after.status }) });
          return after;
        });
        apply();
        return redirect(set as MutableSet, "/admin/dashboard");
      } catch { return errorPage(set as MutableSet, 409, "Connection activation failed."); }
    })
    .post("/admin/meta-connections/:id/egress", ({ request, body, params, set }) => {
      if (!requireMutationSession(request, body, set as MutableSet, adminToken)) return errorPage(set as MutableSet, Number((set as MutableSet).status) || 403, "Authentication or CSRF validation failed.");
      const profileId = formValue(body, "egressProfileId");
      const before = connections.findById(params.id);
      if (!before || !profileId) return errorPage(set as MutableSet, 404, "Connection or egress profile not found.");
      try {
        const apply = db.transaction(() => {
          const after = connections.assignEgress(params.id, profileId);
          audit.record({ tenantId: after.tenantId, actorType: "operator", actorId: "admin-ui", action: "egress.assign", entityType: "meta_connection", entityId: after.id, beforeJson: JSON.stringify({ egressProfileId: before.egressProfileId }), afterJson: JSON.stringify({ egressProfileId: after.egressProfileId }) });
          return after;
        });
        apply();
        return redirect(set as MutableSet, "/admin/dashboard");
      } catch { return errorPage(set as MutableSet, 409, "Egress assignment failed. The profile may be reserved by another live connection."); }
    })
    .post("/admin/meta-connections/:id/chatwoot", ({ request, body, params, set }) => {
      if (!requireMutationSession(request, body, set as MutableSet, adminToken)) return errorPage(set as MutableSet, Number((set as MutableSet).status) || 403, "Authentication or CSRF validation failed.");
      const bindingId = formValue(body, "chatwootBindingId");
      const before = connections.findById(params.id);
      if (!before || !bindingId) return errorPage(set as MutableSet, 404, "Connection or Chatwoot binding not found.");
      try {
        const apply = db.transaction(() => {
          const after = connections.assignChatwootBinding(params.id, bindingId);
          audit.record({ tenantId: after.tenantId, actorType: "operator", actorId: "admin-ui", action: "chatwoot.assign", entityType: "meta_connection", entityId: after.id, beforeJson: JSON.stringify({ chatwootBindingId: before.chatwootBindingId }), afterJson: JSON.stringify({ chatwootBindingId: after.chatwootBindingId }) });
          return after;
        });
        apply();
        return redirect(set as MutableSet, "/admin/dashboard");
      } catch { return errorPage(set as MutableSet, 409, "Chatwoot assignment failed or crossed tenant boundaries."); }
    })
    .post("/admin/egress-profiles", ({ request, body, set }) => {
      if (!requireMutationSession(request, body, set as MutableSet, adminToken)) return errorPage(set as MutableSet, Number((set as MutableSet).status) || 403, "Authentication or CSRF validation failed.");
      const provider = formValue(body, "provider");
      const scheme = normalizeProxyScheme(formValue(body, "scheme"));
      const normalizedHost = normalizeProxyHost(formValue(body, "host"));
      const port = Number(formValue(body, "port"));
      const username = formValue(body, "username") || null;
      const secretRef = formValue(body, "secretRef") || null;
      if (!provider || !scheme || !normalizedHost || !Number.isInteger(port) || port < 1 || port > 65535) return errorPage(set as MutableSet, 400, "Provider, supported scheme, valid host and port are required.");
      if (secretRef && !isSupportedSecretRef(secretRef)) return errorPage(set as MutableSet, 400, "Only env:EGRESS_PROXY_* secret references are accepted.");
      if ((username === null) !== (secretRef === null)) return errorPage(set as MutableSet, 400, "Proxy username and secret reference must be configured together.");
      try {
        const profile = egress.create({ provider, scheme, host: normalizedHost.startsWith("[") ? normalizedHost.slice(1, -1) : normalizedHost, port, username, secretRef, country: formValue(body, "country") || null, region: formValue(body, "region") || null, stickySessionId: null, expectedExitIp: formValue(body, "expectedExitIp") || null, status: "healthy" });
        audit.record({ tenantId: null, actorType: "operator", actorId: "admin-ui", action: "egress.create", entityType: "egress_profile", entityId: profile.id, beforeJson: null, afterJson: JSON.stringify({ provider: profile.provider, scheme: profile.scheme, host: profile.host, port: profile.port, status: profile.status, secretConfigured: !!profile.secretRef }) });
        return redirect(set as MutableSet, "/admin/dashboard");
      } catch { return errorPage(set as MutableSet, 409, "Egress profile could not be created."); }
    })
    .post("/admin/chatwoot-bindings", ({ request, body, set }) => {
      if (!requireMutationSession(request, body, set as MutableSet, adminToken)) return errorPage(set as MutableSet, Number((set as MutableSet).status) || 403, "Authentication or CSRF validation failed.");
      const tenantId = formValue(body, "tenantId");
      const accountId = formValue(body, "chatwootAccountId");
      const inboxId = formValue(body, "chatwootInboxId");
      const apiBaseUrl = formValue(body, "apiBaseUrl").replace(/\/+$/, "");
      const credentialRef = formValue(body, "credentialRef");
      if (!tenantId || !/^[1-9][0-9]*$/.test(accountId) || !/^[1-9][0-9]*$/.test(inboxId) || !credentialRef || !isSupportedChatwootSecretRef(credentialRef)) return errorPage(set as MutableSet, 400, "Valid tenant, numeric Chatwoot IDs and env:CHATWOOT_* credential reference are required.");
      try {
        const parsed = new URL(apiBaseUrl);
        if ((parsed.protocol !== "http:" && parsed.protocol !== "https:") || parsed.username || parsed.password || parsed.search || parsed.hash) return errorPage(set as MutableSet, 400, "Chatwoot base URL must be plain http(s) without credentials, query or fragment.");
      } catch { return errorPage(set as MutableSet, 400, "Chatwoot base URL is invalid."); }
      try {
        const binding = chatwootBindings.create({ tenantId, chatwootAccountId: accountId, chatwootInboxId: inboxId, apiBaseUrl, credentialRef, status: "active" });
        audit.record({ tenantId, actorType: "operator", actorId: "admin-ui", action: "chatwoot.create", entityType: "chatwoot_binding", entityId: binding.id, beforeJson: null, afterJson: JSON.stringify({ accountId, inboxId, status: binding.status, credentialConfigured: true }) });
        return redirect(set as MutableSet, "/admin/dashboard");
      } catch { return errorPage(set as MutableSet, 409, "Chatwoot binding could not be created."); }
    });
}
