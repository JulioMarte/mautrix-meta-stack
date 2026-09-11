import { afterEach, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { createApp } from "../src/app";
import { runMigrations } from "../src/persistence/migrations";
import type { ChatwootBinding, NormalizedMessage } from "../src/domain/models";
import type { ChatwootConversationRef, ChatwootGateway } from "../src/services/chatwoot-gateway";

let db: Database | undefined;
afterEach(() => { db?.close(); db = undefined; });

class RecordingGateway implements ChatwootGateway {
  ensures = 0;
  messages: NormalizedMessage[] = [];
  async ensureConversation(_: { binding: ChatwootBinding; contactIdentifier: string; remoteContactId: string; displayName?: string; remoteThreadId: string }): Promise<ChatwootConversationRef> {
    this.ensures++;
    return { contactId: "5", sourceId: "source-5", conversationId: "9" };
  }
  async createIncomingMessage(input: { binding: ChatwootBinding; conversation: ChatwootConversationRef; sourceEventId: string; text?: string; attachments: NormalizedMessage["attachments"]; normalized: NormalizedMessage }): Promise<{ messageId: string }> {
    this.messages.push(input.normalized);
    return { messageId: "77" };
  }
}

async function post(app: ReturnType<typeof createApp>, path: string, token: string, body: unknown) {
  return app.handle(new Request(`http://localhost${path}`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify(body)
  }));
}

describe("Phase 4 API boundary", () => {
  test("admin binds Chatwoot and authenticated Matrix ingress is idempotent", async () => {
    db = new Database(":memory:", { strict: true });
    runMigrations(db);
    const gateway = new RecordingGateway();
    const admin = "admin-token-123456789";
    const internal = "internal-token-123456789012345";
    const app = createApp(db, admin, internal, { CHATWOOT_TENANT_TOKEN: "canary-chatwoot-secret" }, { chatwootGateway: gateway });

    const tenantResp = await post(app, "/api/v1/tenants", admin, { slug: "phase4", name: "Phase 4" });
    const tenant = (await tenantResp.json()).data;
    const connectionResp = await post(app, "/api/v1/meta-connections", admin, { tenantId: tenant.id, matrixOwnerMxid: "@owner:test", metaAccountId: "123" });
    const connection = (await connectionResp.json()).data;
    const egressResp = await post(app, "/api/v1/egress-profiles", admin, { provider: "fixture", scheme: "http", host: "proxy.test", port: 8080, status: "healthy" });
    const egress = (await egressResp.json()).data;
    expect((await post(app, `/api/v1/meta-connections/${connection.id}/egress`, admin, { egressProfileId: egress.id })).status).toBe(200);

    const bindingResp = await post(app, "/api/v1/chatwoot-bindings", admin, {
      tenantId: tenant.id, chatwootAccountId: "1", chatwootInboxId: "10", apiBaseUrl: "https://chatwoot.test/", credentialRef: "env:CHATWOOT_TENANT_TOKEN"
    });
    expect(bindingResp.status).toBe(201);
    const bindingBody = await bindingResp.json();
    expect(bindingBody.data.credentialRef).toBe("[configured]");
    expect(JSON.stringify(bindingBody)).not.toContain("canary-chatwoot-secret");
    const bindingId = db.query("SELECT id FROM chatwoot_bindings").get() as { id: string };
    expect((await post(app, `/api/v1/meta-connections/${connection.id}/chatwoot`, admin, { chatwootBindingId: bindingId.id })).status).toBe(200);
    expect((await post(app, `/api/v1/meta-connections/${connection.id}/activate`, admin, {})).status).toBe(200);

    const matrixBody = {
      connectionId: connection.id, roomId: "!room:test", remoteThreadId: "thread-1", remoteContactId: "42",
      eventId: "$evt", senderId: "42", senderDisplayName: "Alice", text: "hello",
      occurredAt: "2026-09-11T04:00:00.000Z", provenance: "meta"
    };
    expect((await post(app, "/internal/v1/matrix/events", "wrong-token-but-long-enough-123", matrixBody)).status).toBe(401);
    const first = await post(app, "/internal/v1/matrix/events", internal, matrixBody);
    expect(first.status).toBe(200);
    expect((await first.json()).data.status).toBe("delivered");
    const second = await post(app, "/internal/v1/matrix/events", internal, matrixBody);
    expect(second.status).toBe(200);
    expect((await second.json()).data.status).toBe("duplicate");
    expect(gateway.ensures).toBe(1);
    expect(gateway.messages).toHaveLength(1);
    expect(gateway.messages[0]!.tenantId).toBe(tenant.id);
    expect(gateway.messages[0]!.connectionId).toBe(connection.id);
  });

  test("cross-tenant Chatwoot assignment is rejected at API boundary", async () => {
    db = new Database(":memory:", { strict: true });
    runMigrations(db);
    const admin = "admin-token-123456789";
    const app = createApp(db, admin, "internal-token-123456789012345", {}, { chatwootGateway: new RecordingGateway() });
    const ta = (await (await post(app, "/api/v1/tenants", admin, { slug: "tenant-a", name: "A" })).json()).data;
    const tb = (await (await post(app, "/api/v1/tenants", admin, { slug: "tenant-b", name: "B" })).json()).data;
    const conn = (await (await post(app, "/api/v1/meta-connections", admin, { tenantId: ta.id, matrixOwnerMxid: "@a:test", metaAccountId: "a" })).json()).data;
    await post(app, "/api/v1/chatwoot-bindings", admin, { tenantId: tb.id, chatwootAccountId: "2", chatwootInboxId: "20", apiBaseUrl: "https://chatwoot.test", credentialRef: "env:CHATWOOT_B_TOKEN" });
    const binding = db.query("SELECT id FROM chatwoot_bindings").get() as { id: string };
    const response = await post(app, `/api/v1/meta-connections/${conn.id}/chatwoot`, admin, { chatwootBindingId: binding.id });
    expect(response.status).toBe(409);
    expect((await response.json()).error.code).toBe("CROSS_TENANT_CHATWOOT_BINDING");
  });

  test("disabled historical Chatwoot route is exposed as terminal HTTP 409", async () => {
    db = new Database(":memory:", { strict: true });
    runMigrations(db);
    const gateway = new RecordingGateway();
    const admin = "admin-token-123456789";
    const internal = "internal-token-123456789012345";
    const app = createApp(db, admin, internal, {}, { chatwootGateway: gateway });

    const tenant = (await (await post(app, "/api/v1/tenants", admin, { slug: "migration-api", name: "Migration API" })).json()).data;
    const connection = (await (await post(app, "/api/v1/meta-connections", admin, { tenantId: tenant.id, matrixOwnerMxid: "@owner:test", metaAccountId: "migration-api" })).json()).data;
    const egress = (await (await post(app, "/api/v1/egress-profiles", admin, { provider: "fixture", scheme: "http", host: "proxy.test", port: 8080, status: "healthy" })).json()).data;
    await post(app, `/api/v1/meta-connections/${connection.id}/egress`, admin, { egressProfileId: egress.id });

    const oldBinding = (await (await post(app, "/api/v1/chatwoot-bindings", admin, {
      tenantId: tenant.id, chatwootAccountId: "1", chatwootInboxId: "10", apiBaseUrl: "https://chatwoot.test", credentialRef: "env:CHATWOOT_OLD_TOKEN"
    })).json()).data;
    await post(app, `/api/v1/meta-connections/${connection.id}/chatwoot`, admin, { chatwootBindingId: oldBinding.id });
    await post(app, `/api/v1/meta-connections/${connection.id}/activate`, admin, {});

    const firstBody = {
      connectionId: connection.id, roomId: "!history:test", remoteThreadId: "history-thread", remoteContactId: "42",
      eventId: "$history-1", senderId: "42", text: "first", occurredAt: "2026-09-11T04:00:00.000Z", provenance: "meta"
    };
    expect((await post(app, "/internal/v1/matrix/events", internal, firstBody)).status).toBe(200);

    const newBinding = (await (await post(app, "/api/v1/chatwoot-bindings", admin, {
      tenantId: tenant.id, chatwootAccountId: "1", chatwootInboxId: "20", apiBaseUrl: "https://chatwoot.test", credentialRef: "env:CHATWOOT_NEW_TOKEN"
    })).json()).data;
    await post(app, `/api/v1/meta-connections/${connection.id}/chatwoot`, admin, { chatwootBindingId: newBinding.id });
    db.query("UPDATE chatwoot_bindings SET status = 'disabled' WHERE id = ?").run(oldBinding.id);

    const response = await post(app, "/internal/v1/matrix/events", internal, { ...firstBody, eventId: "$history-2", text: "second" });
    expect(response.status).toBe(409);
    expect((await response.json()).error.code).toBe("CHATWOOT_BINDING_NOT_ACTIVE");
    expect(gateway.messages).toHaveLength(1);
  });
});
