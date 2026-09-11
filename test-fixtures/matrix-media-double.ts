const expectedToken = process.env.MATRIX_MEDIA_CI_TOKEN ?? "";

const fixtures: Record<string, { content: string; type: string }> = {
  photo: { content: "fixture-photo-jpeg", type: "image/jpeg" },
  voice: { content: "fixture-voice-ogg", type: "audio/ogg" },
  pdf: { content: "fixture-pdf-document", type: "application/pdf" }
};

Bun.serve({
  port: 8081,
  fetch(req) {
    const url = new URL(req.url);
    if (url.pathname === "/health") return Response.json({ status: "ok" });
    if (req.headers.get("authorization") !== `Bearer ${expectedToken}` || !expectedToken) {
      return Response.json({ errcode: "M_MISSING_TOKEN", error: "missing or invalid token" }, { status: 401 });
    }
    const prefix = "/_matrix/client/v1/media/download/matrix.example.com/";
    if (!url.pathname.startsWith(prefix)) return Response.json({ errcode: "M_NOT_FOUND", error: "not found" }, { status: 404 });
    const id = decodeURIComponent(url.pathname.slice(prefix.length));
    const fixture = fixtures[id];
    if (!fixture) return Response.json({ errcode: "M_NOT_FOUND", error: "not found" }, { status: 404 });
    return new Response(fixture.content, {
      status: 200,
      headers: {
        "content-type": fixture.type,
        "content-length": String(new TextEncoder().encode(fixture.content).byteLength),
        "content-disposition": `attachment; filename=${id}`
      }
    });
  }
});
