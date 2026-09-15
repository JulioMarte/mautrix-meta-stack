type Hit = { method: string; target: string };

const label = process.env.EGRESS_LABEL ?? "unknown";
const hits: Hit[] = [];
let unavailable = false;

function json(value: unknown, status = 200) {
  return Response.json(value, { status });
}

Bun.serve({
  port: 8081,
  async fetch(req) {
    const url = new URL(req.url);
    if (url.pathname === "/health") return json({ status: "ok", label });
    if (url.pathname === "/_test/state") return json({ label, unavailable, hits });
    if (url.pathname === "/_test/down" && req.method === "POST") {
      unavailable = true;
      return json({ ok: true });
    }
    if (url.pathname === "/_test/up" && req.method === "POST") {
      unavailable = false;
      return json({ ok: true });
    }
    if (url.pathname === "/_test/reset" && req.method === "POST") {
      hits.splice(0, hits.length);
      unavailable = false;
      return json({ ok: true });
    }

    if (unavailable) return json({ error: "egress unavailable" }, 502);

    // This is intentionally a terminating HTTP proxy test double. It records the
    // absolute target selected by the caller and returns a synthetic upstream
    // response without forwarding. A direct-network sentinel therefore remains
    // untouched when the protected request actually used this proxy.
    hits.push({ method: req.method, target: req.url });
    return new Response(`egress:${label}`, {
      status: 200,
      headers: { "content-type": "text/plain", "x-egress-label": label }
    });
  }
});
