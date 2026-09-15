let hits = 0;

function json(value: unknown, status = 200) {
  return Response.json(value, { status });
}

Bun.serve({
  port: 8083,
  fetch(req) {
    const url = new URL(req.url);
    if (url.pathname === "/health") return json({ status: "ok" });
    if (url.pathname === "/_test/state") return json({ hits });
    if (url.pathname === "/_test/reset" && req.method === "POST") {
      hits = 0;
      return json({ ok: true });
    }
    hits += 1;
    return new Response("DIRECT-EGRESS-SENTINEL", { status: 200, headers: { "content-type": "text/plain" } });
  }
});
