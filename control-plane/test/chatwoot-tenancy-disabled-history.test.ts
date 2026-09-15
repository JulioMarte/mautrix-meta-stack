import { afterEach, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import type { ChatwootBinding, NormalizedMessage } from "../src/domain/models";
import { runMigrations } from "../src/persistence/migrations";
import {
  SQLiteChatwootBindingRepository, SQLiteConversationBindingRepository, SQLiteMetaConnectionRepository,
  SQLiteProcessedEventRepository, SQLiteTenantRepository
} from "../src/persistence/sqlite-repositories";
import type { ChatwootConversationRef, ChatwootGateway } from "../src/services/chatwoot-gateway";
import { MatrixToChatwootService } from "../src/services/matrix-to-chatwoot";

let db: Database | undefined;
afterEach(() => { db?.close(); db = undefined; });

class Gateway implements ChatwootGateway {
  messages = 0;
  async ensureConversation(_input: { binding: ChatwootBinding; contactIdentifier: string; remoteContactId: string; displayName?: string; remoteThreadId: string }): Promise<ChatwootConversationRef> {
    return { contactId: "5", sourceId: "source", conversationId: "77" };
  }
  async createIncomingMessage(_input: { binding: ChatwootBinding; conversation: ChatwootConversationRef; sourceEventId: string; text?: string; attachments: NormalizedMessage["attachments"]; normalized: NormalizedMessage }): Promise<{ messageId: string }> {
    this.messages++;
    return { messageId: `message-${this.messages}` };
  }
}

describe("historical Chatwoot route lifecycle", () => {
  test("a disabled historical binding fails terminally instead of retrying or using the new inbox", async () => {
    db = new Database(":memory:", { strict: true });
    runMigrations(db);
    const tenants = new SQLiteTenantRepository(db);
    const connections = new SQLiteMetaConnectionRepository(db);
    const bindings = new SQLiteChatwootBindingRepository(db);
    const conversations = new SQLiteConversationBindingRepository(db);
    const processed = new SQLiteProcessedEventRepository(db);
    const gateway = new Gateway();
    const service = new MatrixToChatwootService(tenants, connections, bindings, conversations, processed, gateway);

    const tenant = tenants.create({ slug: "history", name: "History" });
    const oldBinding = bindings.create({ tenantId: tenant.id, chatwootAccountId: "1", chatwootInboxId: "10", apiBaseUrl: "https://chatwoot.example.com", credentialRef: "env:CHATWOOT_HISTORY_A", status: "active" });
    const connection = connections.create({ tenantId: tenant.id, matrixOwnerMxid: "@owner:matrix.example.com", metaAccountId: "42" });
    connections.assignChatwootBinding(connection.id, oldBinding.id);
    connections.setStatus(connection.id, "active");

    const first = {
      connectionId: connection.id, roomId: "!room:matrix.example.com", remoteThreadId: "thread", remoteContactId: "remote",
      eventId: "$one", senderId: "remote", text: "first", occurredAt: "2026-09-11T12:00:00.000Z", provenance: "meta" as const
    };
    await service.handle(first);
    expect(gateway.messages).toBe(1);

    const newBinding = bindings.create({ tenantId: tenant.id, chatwootAccountId: "1", chatwootInboxId: "20", apiBaseUrl: "https://chatwoot.example.com", credentialRef: "env:CHATWOOT_HISTORY_B", status: "active" });
    connections.assignChatwootBinding(connection.id, newBinding.id);
    bindings.setStatus(oldBinding.id, "disabled");

    const second = { ...first, eventId: "$two", text: "second" };
    await expect(service.handle(second)).rejects.toThrow("CHATWOOT_BINDING_NOT_ACTIVE");
    expect(processed.find("matrix", "$two")?.status).toBe("failed_terminal");
    expect(gateway.messages).toBe(1);
  });
});
