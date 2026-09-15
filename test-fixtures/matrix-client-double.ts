type Upload = { mxc: string; contentType: string; filename: string; content: string };
type EventRecord = { roomId: string; txnId: string; eventId: string; content: Record<string, unknown> };

const expectedToken = process.env.MATRIX_CLIENT_CI_TOKEN ?? "";
const uploads: Upload[] = [];
const events: EventRecord[] = [];
const byTxn = new Map<string, EventRecord>();
let failAfterNextSend = false;
let nextUpload = 1;
let nextEvent = 1;

function json(value: unknown, status = 200) { return Response.json(value, { status }); }
function authorized(req: Request) { return expectedToken.length > 0 && req.headers.get("authorization") === `Bearer ${expectedToken}`; }

Bun.serve({
  port: 8082,
  async fetch(req) {
    const url = new URL(req.url);
    if (url.pathname === "/health") return json({ status: "ok" });
    if (url.pathname === "/_test/state") return json({ uploads, events });
    if (url.pathname === "/_test/fail-after-next-send" && req.method === "POST") { failAfterNextSend = true; return json({ ok: true }); }
    if (!authorized(req)) return json({ errcode: "M_UNKNOWN_TOKEN" }, 401);

    if (url.pathname === "/_matrix/media/v3/upload" && req.method === "POST") {
      const bytes = new Uint8Array(await req.arrayBuffer());
      const mxc = `mxc://matrix-double/upload-${nextUpload++}`;
      uploads.push({
        mxc,
        contentType: req.headers.get("content-type") ?? "application/octet-stream",
        filename: url.searchParams.get("filename") ?? "",
        content: Buffer.from(bytes).toString("utf8")
      });
      return json({ content_uri: mxc });
    }

    const match = /^\/_matrix\/client\/v3\/rooms\/([^/]+)\/send\/m\.room\.message\/([^/]+)$/.exec(url.pathname);
    if (match && req.method === "PUT") {
      const roomId = decodeURIComponent(match[1]!);
      const txnId = decodeURIComponent(match[2]!);
      let record = byTxn.get(txnId);
      if (!record) {
        const content = await req.json() as Record<string, unknown>;
        record = { roomId, txnId, eventId: `$phase5-${nextEvent++}`, content };
        byTxn.set(txnId, record);
        events.push(record);
      }
      if (failAfterNextSend) {
        failAfterNextSend = false;
        return json({ errcode: "M_UNKNOWN", error: "injected post-commit failure" }, 500);
      }
      return json({ event_id: record.eventId });
    }

    return json({ errcode: "M_NOT_FOUND" }, 404);
  }
});
