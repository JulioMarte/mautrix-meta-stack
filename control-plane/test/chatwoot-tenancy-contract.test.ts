import { afterEach, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import type { ChatwootBinding, NormalizedMessage } from "../src/domain/models";
import { runMigrations } from "../src/persistence/migrations";
import {
  SQLiteChatwootBindingRepository, SQLiteConversationBindingRepository, SQLiteMetaConnectionRepository,
  SQLiteProcessedEventRepository, SQLiteTenantRepository
} from "../src/persistence/sqlite-repositories";
import { ChatwootAttachmentDownloader } from "../src/services/chatwoot-attachment-downloader";
import { deterministicChatwootContactIdentifier, type ChatwootConversationRef, type ChatwootGateway } from "../src/services/chatwoot-gateway";
import { ChatwootToMatrixService } from "../src/services/chatwoot-to-matrix";
import type { MatrixGateway } from "../src/services/matrix-gateway";
import { MatrixToChatwootService, type MatrixInboundEvent } from "../src/services/matrix-to-chatwoot";

let db: Database | undefined;
afterEach(() => { db?.close(); db = undefined; });

class CollisionGateway implements ChatwootGateway {
  ensureCalls: Array<{ binding: ChatwootBinding; contactIdentifier: string; remoteContactId: string; remoteThreadId: string }> = [];
  messageCalls: Array<{ binding: ChatwootBinding; conversation: ChatwootConversationRef; normalized: NormalizedMessage }> = [];

  async ensureConversation(input: { binding: ChatwootBinding; contactIdentifier: string; remoteContactId: string; displayName?: string; remoteThreadId: string }): Promise<ChatwootConversationRef> {
    this.ensureCalls.push({ binding: input.binding, contactIdentifier: input.contactIdentifier, remoteContactId: input.remoteContactId, remoteThreadId: input.remoteThreadId });
    // Deliberately collide downstream-looking numeric IDs across tenant contexts.
    return { contactId: "5", sourceId: "source-5", conversationId: "77" };
  }

  async createIncomingMessage(input: { binding: ChatwootBinding; conversation: ChatwootConversationRef; sourceEventId: string; text?: string; attachments: NormalizedMessage["attachments"]; normalized: NormalizedMessage }): Promise<{ messageId: string }> {
    this.messageCalls.push({ binding: input.binding, conversation: input.conversation, normalized: input.normalized });
    return { messageId: `msg-${input.sourceEventId}` };
  }
}

function matrixEvent(connectionId: string, roomId: string, remoteThreadId: string, eventId: string, remoteContactId = "42"): MatrixInboundEvent {
  return {
    connectionId, roomId, remoteThreadId, remoteContactId, eventId,
    senderId: remoteContactId, senderDisplayName: "Remote", text: `text-${eventId}`,
    occurredAt: "2026-09-11T12:00:00.000Z", provenance: "meta"
  };
}

function setup() {
  db = new Database(":memory:", { strict: true });
  runMigrations(db);
  const tenants = new SQLiteTenantRepository(db);
  const connections = new SQLiteMetaConnectionRepository(db);
  const bindings = new SQLiteChatwootBindingRepository(db);
  const conversations = new SQLiteConversationBindingRepository(db);
  const processed = new SQLiteProcessedEventRepository(db);
  const gateway = new CollisionGateway();
  const inbound = new MatrixToChatwootService(tenants, connections, bindings, conversations, processed, gateway);
  const matrixCalls: Array<{ roomId: string; sourceEventId: string }> = [];
  const matrix: MatrixGateway = {
    async send(input) {
      matrixCalls.push({ roomId: input.roomId, sourceEventId: input.sourceEventId });
      return { eventIds: [`$${matrixCalls.length}`] };
    }
  };
  const downloader = new ChatwootAttachmentDownloader({ resolve: () => "token" }, {}, async () => new Response("bytes"));
  const outbound = new ChatwootToMatrixService(tenants, connections, bindings, conversations, processed, downloader, matrix);

  function route(slug: string, apiBaseUrl: string, accountId = "1", inboxId = "10") {
    const tenant = tenants.create({ slug, name: slug });
    const binding = bindings.create({
      tenantId: tenant.id, chatwootAccountId: accountId, chatwootInboxId: inboxId,
      apiBaseUrl, credentialRef: `env:CHATWOOT_${slug.toUpperCase().replaceAll("-", "_")}_TOKEN`, status: "active"
    });
    const connection = connections.create({ tenantId: tenant.id, matrixOwnerMxid: `@${slug}:matrix.example.com`, metaAccountId: `meta-${slug}` });
    connections.assignChatwootBinding(connection.id, binding.id);
    connections.setStatus(connection.id, "active");
    return { tenant, binding, connection: connections.findById(connection.id)! };
  }

  return { tenants, connections, bindings, conversations, processed, gateway, inbound, outbound, matrixCalls, route };
}

describe("Chatwoot tenancy contract", () => {
  test("colliding numeric Chatwoot IDs remain tenant-isolated in both directions", async () => {
    const f = setup();
    const a = f.route("tenant-a", "https://chatwoot-a.example.com");
    const b = f.route("tenant-b", "https://chatwoot-b.example.com");

    await f.inbound.handle(matrixEvent(a.connection.id, "!a:matrix.example.com", "thread", "$a"));
    await f.inbound.handle(matrixEvent(b.connection.id, "!b:matrix.example.com", "thread", "$b"));

    expect(f.gateway.ensureCalls).toHaveLength(2);
    expect(f.gateway.ensureCalls[0]!.binding.apiBaseUrl).toBe("https://chatwoot-a.example.com");
    expect(f.gateway.ensureCalls[1]!.binding.apiBaseUrl).toBe("https://chatwoot-b.example.com");
    expect(f.gateway.ensureCalls[0]!.contactIdentifier).not.toBe(f.gateway.ensureCalls[1]!.contactIdentifier);

    await f.outbound.handle(a.binding, { messageId: "same-id", accountId: "1", inboxId: "10", conversationId: "77", senderId: "8", text: "A reply", attachments: [], occurredAt: "2026-09-11T12:01:00.000Z" });
    await f.outbound.handle(b.binding, { messageId: "same-id", accountId: "1", inboxId: "10", conversationId: "77", senderId: "8", text: "B reply", attachments: [], occurredAt: "2026-09-11T12:01:00.000Z" });

    expect(f.matrixCalls).toEqual([
      { roomId: "!a:matrix.example.com", sourceEventId: `${a.binding.id}:same-id` },
      { roomId: "!b:matrix.example.com", sourceEventId: `${b.binding.id}:same-id` }
    ]);
  });

  test("same remote contact ID is deterministically isolated by tenant and connection", () => {
    const a = deterministicChatwootContactIdentifier({ tenantId: "tenant-a", connectionId: "connection-a", remoteContactId: "42" });
    const b = deterministicChatwootContactIdentifier({ tenantId: "tenant-b", connectionId: "connection-b", remoteContactId: "42" });
    expect(a).not.toBe(b);
  });

  test("changing the active inbox preserves historical conversations and uses the new inbox only for new threads", async () => {
    const f = setup();
    const route = f.route("migration", "https://chatwoot.example.com", "1", "10");

    await f.inbound.handle(matrixEvent(route.connection.id, "!old:matrix.example.com", "old-thread", "$old-1"));
    const oldConversation = f.conversations.findByRemoteThread(route.connection.id, "old-thread")!;
    expect(f.gateway.messageCalls.at(-1)!.binding.id).toBe(route.binding.id);

    const bindingB = f.bindings.create({
      tenantId: route.tenant.id, chatwootAccountId: "1", chatwootInboxId: "20", apiBaseUrl: "https://chatwoot.example.com",
      credentialRef: "env:CHATWOOT_MIGRATION_B_TOKEN", status: "active"
    });
    f.connections.assignChatwootBinding(route.connection.id, bindingB.id);

    await f.inbound.handle(matrixEvent(route.connection.id, "!old:matrix.example.com", "old-thread", "$old-2"));
    expect(f.gateway.messageCalls.at(-1)!.binding.id).toBe(route.binding.id);
    expect(f.conversations.findByRemoteThread(route.connection.id, "old-thread")!.chatwootInboxId).toBe("10");

    await f.inbound.handle(matrixEvent(route.connection.id, "!new:matrix.example.com", "new-thread", "$new-1"));
    expect(f.gateway.messageCalls.at(-1)!.binding.id).toBe(bindingB.id);
    expect(f.conversations.findByRemoteThread(route.connection.id, "new-thread")!.chatwootInboxId).toBe("20");

    await f.outbound.handle(route.binding, {
      messageId: "old-agent", accountId: oldConversation.chatwootAccountId, inboxId: oldConversation.chatwootInboxId,
      conversationId: oldConversation.chatwootConversationId, senderId: "agent", text: "historical reply", attachments: [], occurredAt: "2026-09-11T12:02:00.000Z"
    });
    expect(f.matrixCalls.at(-1)!.roomId).toBe("!old:matrix.example.com");
  });

  test("a different binding cannot claim a historical conversation", async () => {
    const f = setup();
    const route = f.route("cross-binding", "https://chatwoot.example.com", "1", "10");
    await f.inbound.handle(matrixEvent(route.connection.id, "!bound:matrix.example.com", "thread", "$bound"));

    const other = f.bindings.create({
      tenantId: route.tenant.id, chatwootAccountId: "1", chatwootInboxId: "20", apiBaseUrl: "https://chatwoot.example.com",
      credentialRef: "env:CHATWOOT_CROSS_BINDING_OTHER_TOKEN", status: "active"
    });
    await expect(f.outbound.handle(other, {
      messageId: "wrong-binding", accountId: "1", inboxId: "20", conversationId: "77", senderId: "agent", text: "wrong", attachments: [], occurredAt: "2026-09-11T12:03:00.000Z"
    })).rejects.toThrow("CONVERSATION_BINDING_NOT_FOUND");
    expect(f.matrixCalls).toHaveLength(0);
  });

  test("a tenant cannot use another tenant binding even when numeric IDs collide", async () => {
    const f = setup();
    const a = f.route("owner-a", "https://chatwoot-a.example.com");
    const b = f.route("owner-b", "https://chatwoot-b.example.com");
    await f.inbound.handle(matrixEvent(a.connection.id, "!a:matrix.example.com", "thread-a", "$a-event"));

    await expect(f.outbound.handle(b.binding, {
      messageId: "cross-tenant", accountId: "1", inboxId: "10", conversationId: "77", senderId: "agent", text: "wrong tenant", attachments: [], occurredAt: "2026-09-11T12:04:00.000Z"
    })).rejects.toThrow("CONVERSATION_BINDING_NOT_FOUND");
    expect(f.matrixCalls).toHaveLength(0);
  });
});
