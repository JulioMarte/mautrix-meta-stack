import { afterEach, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { runMigrations } from "../src/persistence/migrations";
import {
  SQLiteChatwootBindingRepository, SQLiteConversationBindingRepository, SQLiteMetaConnectionRepository,
  SQLiteProcessedEventRepository, SQLiteTenantRepository
} from "../src/persistence/sqlite-repositories";
import { ChatwootAttachmentDownloader } from "../src/services/chatwoot-attachment-downloader";
import { ChatwootToMatrixService } from "../src/services/chatwoot-to-matrix";
import { HttpMatrixGateway, type MatrixGateway } from "../src/services/matrix-gateway";

let db: Database | undefined;
afterEach(() => { db?.close(); db = undefined; });

function fixture(matrix: MatrixGateway) {
  db = new Database(":memory:", { strict: true });
  runMigrations(db);
  const tenants = new SQLiteTenantRepository(db);
  const connections = new SQLiteMetaConnectionRepository(db);
  const chatwoot = new SQLiteChatwootBindingRepository(db);
  const conversations = new SQLiteConversationBindingRepository(db);
  const processed = new SQLiteProcessedEventRepository(db);
  const tenant = tenants.create({ slug: "phase5", name: "Phase 5" });
  const binding = chatwoot.create({
    tenantId: tenant.id, chatwootAccountId: "1", chatwootInboxId: "10", apiBaseUrl: "https://chatwoot.example.com",
    credentialRef: "env:CHATWOOT_TEST_TOKEN", status: "active"
  });
  const connection = connections.create({ tenantId: tenant.id, matrixOwnerMxid: "@owner:matrix.example.com", metaAccountId: "42" });
  connections.assignChatwootBinding(connection.id, binding.id);
  connections.setStatus(connection.id, "active");
  conversations.create({
    tenantId: tenant.id, metaConnectionId: connection.id, matrixRoomId: "!room:matrix.example.com", remoteThreadId: "thread",
    remoteContactId: "remote", chatwootAccountId: "1", chatwootInboxId: "10", chatwootContactId: "5", chatwootSourceId: "source",
    chatwootConversationId: "77"
  });
  const downloader = new ChatwootAttachmentDownloader({ resolve: () => "api-token" }, {}, async () => new Response("file"));
  return {
    binding,
    processed,
    service: new ChatwootToMatrixService(tenants, connections, chatwoot, conversations, processed, downloader, matrix)
  };
}

function event(id = "900") {
  return {
    messageId: id, accountId: "1", inboxId: "10", conversationId: "77", senderId: "8", senderName: "Agent",
    text: "reply", attachments: [], occurredAt: "2026-09-11T12:00:00.000Z"
  };
}

describe("Phase 5 Chatwoot -> Matrix service", () => {
  test("routes an outgoing agent reply to the exact bound Matrix room and suppresses duplicate webhook delivery", async () => {
    const calls: Array<Record<string, unknown>> = [];
    const matrix: MatrixGateway = { async send(input) { calls.push(input); return { eventIds: ["$matrix-event"] }; } };
    const f = fixture(matrix);
    const first = await f.service.handle(f.binding, event());
    const duplicate = await f.service.handle(f.binding, event());
    expect(first.status).toBe("delivered");
    expect(duplicate.status).toBe("duplicate");
    expect(calls).toHaveLength(1);
    expect(calls[0]).toMatchObject({ roomId: "!room:matrix.example.com", text: "reply", sourceEventId: `${f.binding.id}:900` });
  });

  test("route mismatch fails before a Matrix side effect", async () => {
    let sends = 0;
    const f = fixture({ async send() { sends++; return { eventIds: ["$x"] }; } });
    await expect(f.service.handle(f.binding, { ...event(), inboxId: "99" })).rejects.toThrow("CHATWOOT_ROUTE_MISMATCH");
    expect(sends).toBe(0);
  });

  test("a retryable Matrix failure reuses the deterministic transaction base", async () => {
    const txn: string[] = [];
    let attempt = 0;
    const f = fixture({ async send(input) { txn.push(input.transactionBase); if (attempt++ === 0) throw new Error("MATRIX_CLIENT_TIMEOUT"); return { eventIds: ["$ok"] }; } });
    await expect(f.service.handle(f.binding, event("901"))).rejects.toThrow("MATRIX_CLIENT_TIMEOUT");
    const retry = await f.service.handle(f.binding, event("901"));
    expect(retry.status).toBe("delivered");
    expect(txn).toHaveLength(2);
    expect(txn[0]).toBe(txn[1]);
  });
});

describe("Phase 5 Matrix HTTP gateway", () => {
  test("uploads binary media then sends with a deterministic Matrix transaction ID and provenance marker", async () => {
    const calls: Array<{ url: URL; init?: RequestInit }> = [];
    const fetchStub = async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
      const url = new URL(String(input)); calls.push(init === undefined ? { url } : { url, init });
      if (url.pathname === "/_matrix/media/v3/upload") return Response.json({ content_uri: "mxc://matrix.example.com/uploaded" });
      return Response.json({ event_id: "$event" });
    };
    const gateway = new HttpMatrixGateway({ MATRIX_CLIENT_BASE_URL: "https://matrix.example.com", MATRIX_CLIENT_ACCESS_TOKEN: "matrix-token" }, fetchStub);
    const result = await gateway.send({
      roomId: "!room:matrix.example.com", transactionBase: "cw-deadbeef", sourceEventId: "binding:900", attachments: [{
        kind: "file", url: "data:application/pdf;base64,UERG", mimeType: "application/pdf", fileName: "invoice.pdf", sizeBytes: 3
      }]
    });
    expect(result.eventIds).toEqual(["$event"]);
    expect(calls).toHaveLength(2);
    expect(calls[0]!.url.pathname).toBe("/_matrix/media/v3/upload");
    expect(calls[1]!.url.pathname).toContain("/send/m.room.message/cw-deadbeef-a0");
    expect(new Headers(calls[0]!.init?.headers).get("authorization")).toBe("Bearer matrix-token");
    const sent = JSON.parse(String(calls[1]!.init?.body));
    expect(sent).toMatchObject({ msgtype: "m.file", url: "mxc://matrix.example.com/uploaded", "com.mautrix_meta_stack.provenance": { source: "chatwoot", source_event_id: "binding:900" } });
  });
});

describe("Phase 5 Chatwoot attachment download", () => {
  test("strips the Chatwoot API token on an HTTPS cross-origin Active Storage redirect", async () => {
    const auth: Array<string | null> = [];
    let call = 0;
    const fetchStub = async (_input: string | URL | Request, init?: RequestInit): Promise<Response> => {
      auth.push(new Headers(init?.headers).get("api_access_token"));
      if (call++ === 0) return new Response(null, { status: 302, headers: { location: "https://storage.example.net/object" } });
      return new Response("pdf-bytes", { status: 200, headers: { "content-type": "application/pdf" } });
    };
    const downloader = new ChatwootAttachmentDownloader({ resolve: () => "chatwoot-secret" }, {}, fetchStub);
    const result = await downloader.download({
      id: "b", tenantId: "t", chatwootAccountId: "1", chatwootInboxId: "10", apiBaseUrl: "https://chatwoot.example.com", credentialRef: "env:CHATWOOT_X", status: "active", createdAt: "x", updatedAt: "x"
    }, { id: "1", kind: "file", dataUrl: "https://chatwoot.example.com/rails/active_storage/blobs/1", mimeType: "application/pdf", fileName: "invoice.pdf" });
    expect(auth).toEqual(["chatwoot-secret", null]);
    expect(result.url.startsWith("data:application/pdf;base64,")).toBe(true);
  });
});
