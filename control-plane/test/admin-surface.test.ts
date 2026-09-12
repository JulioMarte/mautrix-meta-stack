import { afterEach, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { createAdminSurface } from "../src/admin-surface";
import { runMigrations } from "../src/persistence/migrations";
import { SQLiteAuditRepository, SQLiteTenantRepository } from "../src/persistence/sqlite-repositories";

const ADMIN_TOKEN = "test-admin-token-123456";
let db: Database | undefined;

afterEach(() => {
  db?.close();
  db = undefined;
});

function freshDb() {
  db = new Database(":memory:", { strict: true });
  runMigrations(db);
  return db;
}

function cookieFrom(response: Response): string {
  const setCookie = response.headers.get("set-cookie") ?? "";
  return setCookie.split(";", 1)[0] ?? "";
}

async function login(app: ReturnType<typeof createAdminSurface>) {
  const response = await app.handle(new Request("http://localhost/admin/login", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ token: ADMIN_TOKEN })
  }));
  return { response, cookie: cookieFrom(response) };
}

async function csrfFromDashboard(app: ReturnType<typeof createAdminSurface>, cookie: string): Promise<{ response: Response; csrf: string; html: string }> {
  const response = await app.handle(new Request("http://localhost/admin/dashboard", { headers: { cookie } }));
  const html = await response.text();
  const match = html.match(/name="csrf" value="([^"]+)"/);
  return { response, csrf: match?.[1] ?? "", html };
}

describe("server-rendered admin surface", () => {
  test("login exchanges admin token for an opaque hardened session cookie", async () => {
    const app = createAdminSurface(freshDb(), ADMIN_TOKEN);
    const { response, cookie } = await login(app);

    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toBe("/admin/dashboard");
    const setCookie = response.headers.get("set-cookie") ?? "";
    expect(setCookie).toContain("HttpOnly");
    expect(setCookie).toContain("Secure");
    expect(setCookie).toContain("SameSite=Strict");
    expect(cookie).toStartWith("mcp_admin_session=");
    expect(setCookie).not.toContain(ADMIN_TOKEN);
  });

  test("dashboard requires authentication and bearer bootstrap never renders the token", async () => {
    const app = createAdminSurface(freshDb(), ADMIN_TOKEN);
    const unauthorized = await app.handle(new Request("http://localhost/admin/dashboard"));
    expect(unauthorized.status).toBe(401);

    const authorized = await app.handle(new Request("http://localhost/admin/dashboard", {
      headers: { authorization: `Bearer ${ADMIN_TOKEN}` }
    }));
    expect(authorized.status).toBe(200);
    expect(authorized.headers.get("set-cookie")).toContain("mcp_admin_session=");
    expect(await authorized.text()).not.toContain(ADMIN_TOKEN);
  });

  test("state-changing forms reject missing CSRF and accept the session-bound token", async () => {
    const database = freshDb();
    const app = createAdminSurface(database, ADMIN_TOKEN);
    const { cookie } = await login(app);
    const dashboard = await csrfFromDashboard(app, cookie);
    expect(dashboard.response.status).toBe(200);
    expect(dashboard.csrf.length).toBeGreaterThan(20);

    const rejected = await app.handle(new Request("http://localhost/admin/tenants", {
      method: "POST",
      headers: { cookie, "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ slug: "tenant-a", name: "Tenant A" })
    }));
    expect(rejected.status).toBe(403);

    const accepted = await app.handle(new Request("http://localhost/admin/tenants", {
      method: "POST",
      headers: { cookie, "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ csrf: dashboard.csrf, slug: "tenant-a", name: "Tenant A" })
    }));
    expect(accepted.status).toBe(303);
    expect(new SQLiteTenantRepository(database).list()).toHaveLength(1);
    expect(new SQLiteAuditRepository(database).listForEntity("tenant", new SQLiteTenantRepository(database).list()[0]!.id).some((event) => event.action === "tenant.create")).toBe(true);
  });

  test("egress secret references are accepted for configuration but never rendered back", async () => {
    const database = freshDb();
    const app = createAdminSurface(database, ADMIN_TOKEN);
    const { cookie } = await login(app);
    const { csrf } = await csrfFromDashboard(app, cookie);
    const secretRef = "env:EGRESS_PROXY_TEST_PASSWORD";

    const create = await app.handle(new Request("http://localhost/admin/egress-profiles", {
      method: "POST",
      headers: { cookie, "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        csrf,
        provider: "test-provider",
        scheme: "socks5",
        host: "proxy.example.test",
        port: "1080",
        username: "proxy-user",
        secretRef,
        country: "US",
        expectedExitIp: "203.0.113.10"
      })
    }));
    expect(create.status).toBe(303);

    const rendered = await csrfFromDashboard(app, cookie);
    expect(rendered.response.status).toBe(200);
    expect(rendered.html).toContain("configured");
    expect(rendered.html).not.toContain(secretRef);
    expect(rendered.html).not.toContain("proxy-user");
  });

  test("logout is CSRF-protected and expires the admin session", async () => {
    const app = createAdminSurface(freshDb(), ADMIN_TOKEN);
    const { cookie } = await login(app);
    const { csrf } = await csrfFromDashboard(app, cookie);
    const response = await app.handle(new Request("http://localhost/admin/logout", {
      method: "POST",
      headers: { cookie, "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ csrf })
    }));

    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toBe("/admin/login");
    expect(response.headers.get("set-cookie")).toContain("Max-Age=0");
  });
});
