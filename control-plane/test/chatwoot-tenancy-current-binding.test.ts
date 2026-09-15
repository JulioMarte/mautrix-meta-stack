import { afterEach, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import type { ChatwootBinding, NormalizedMessage } from "../src/domain/models";
import { runMigrations } from "../src/persistence/migrations";
import {
  SQLiteChatwootBindingRepository, SQLiteConversationBindingRepository, SQLiteMetaConnectionRepository,
  SQLiteProcessedEventRepository, SQLiteTenantRepository
} from "../src/persistence/sqlite-repositories";
import type { ChatwootConversationRef, ChatwootGateway } from "../src/services/chatwoot-gateway";
import { MatrixToChatwootService, type MatrixInboundEvent } from "../src/services/matrix-to-chatwoot";

let db: Database | undefined;
afterEach(() => { db?.close(); db = undefined; });

class Gateway implements ChatwootGateway {
  messages: ChatwootBinding[] = [];
  async ensureConversation(input: { binding: ChatwootBinding; contactIdentifier: string; remoteContactId: string; displayName?: string; remoteThreadId: string }): Promise<ChatwootConversationRef> {
    return { contactId: "5", sourceId: "source", conversationId: input.remoteThreadId === "old-thread" ? "77" : "88" };
  }
  async createIncomingMessage(input: { binding: ChatwootBinding; conversation: ChatwootConversationRef; sourceEventId: string; text?: string; attachments: NormalizedMessage["attachments"]; normalized: NormalizedMessage }): Promise<{ messageId: string }> {
    this.messages.push(input.binding);
    return { messageId: `message-${this.messages.length}` };
  }
}

function event(connectionId: string, roomId: string, threadId: string, eventId: string): MatrixInboundEvent {
  return {
    connectionId, roomId, remoteThreadId: threadId, remoteContactId: "remote", eventId,
    senderId: "remote", text: eventId, occurredAt: "2026-09-11T12:00:00.000Z", provenance: "meta"
  };
}

describe("current versus historical Chatwoot binding authority", () => {
  test("disabled current binding blocks new threads but not an active historical route", async () => {
    db = new Database(":memory:", { strict: true });
    runMigrations(db);
    const tenants = new SQLiteTenantRepository(db);
    const connections = new SQLiteMetaConnectionRepository(db);
    const bindings = new SQLiteChatwootBindingRepository(db);
    const conversations = new SQLiteConversationBindingRepository(db);
    const processed = new SQLiteProcessedEventRepository(db);
    const gateway = new Gateway();
    const service = new MatrixToChatwootService(tenants, connections, bindings, conversations, processed, gateway);

    const tenant = tenants.create({ slug: "authority", name: "Authority" });
    const bindingA = bindings.create({
      tenantId: tenant.id, chatwootAccountId: "1", chatwootInboxId: "10", apiBaseUrl: "https://chatwoot.example.com",
      credentialRef: "env:CHATWOOT_AUTHORITY_A", status: "active"
    });
    const connection = connections.create({ tenantId: tenant.id, matrixOwnerMxid: "@owner:matrix.example.com", metaAccountId: "42" });
    connections.assignChatwootBinding(connection.id, bindingA.id);
    connections.setStatus(connection.id, "active");

    await service.handle(event(connection.id, "!old:matrix.example.com", "old-thread", "$old-1"));
    expect(gateway.messages.at(-1)!.id).toBe(bindingA.id);

    const bindingB = bindings.create({
      tenantId: tenant.id, chatwootAccountId: "1", chatwootInboxId: "20", apiBaseUrl: "https://chatwoot.example.com",
      credentialRef: "env:CHATWOOT_AUTHORITY_B", status: "active"
    });
    connections.assignChatwootBinding(connection.id, bindingB.id);
    bindings.setStatus(bindingB.id, "disabled");

    await service.handle(event(connection.id, "!old:matrix.example.com", "old-thread", "$old-2"));
    expect(gateway.messages).toHaveLength(2);
    expect(gateway.messages.at(-1)!.id).toBe(bindingA.id);

    await expect(service.handle(event(connection.id, "!new:matrix.example.com", "new-thread", "$new-1"))).rejects.toThrow("CHATWOOT_BINDING_NOT_ACTIVE");
    expect(processed.find("matrix", "$new-1")?.status).toBe("failed_terminal");
    expect(gateway.messages).toHaveLength(2);
  });
});
