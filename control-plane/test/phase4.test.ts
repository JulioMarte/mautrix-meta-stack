import { afterEach, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { runMigrations } from "../src/persistence/migrations";
import {
  SQLiteChatwootBindingRepository, SQLiteConversationBindingRepository, SQLiteEgressProfileRepository,
  SQLiteMetaConnectionRepository, SQLiteProcessedEventRepository, SQLiteTenantRepository
} from "../src/persistence/sqlite-repositories";
import { deterministicChatwootContactIdentifier, type ChatwootGateway, type ChatwootConversationRef } from "../src/services/chatwoot-gateway";
import { MatrixToChatwootService, type MatrixInboundEvent } from "../src/services/matrix-to-chatwoot";
import type { ChatwootBinding, NormalizedMessage } from "../src/domain/models";

let db: Database | undefined;
afterEach(() => { db?.close(); db = undefined; });

class FakeChatwootGateway implements ChatwootGateway {
  ensureCalls: Array<{ binding: ChatwootBinding; contactIdentifier: string; remoteContactId: string; remoteThreadId: string }> = [];
  messageCalls: Array<{ binding: ChatwootBinding; conversation: ChatwootConversationRef; sourceEventId: string; normalized: NormalizedMessage }> = [];
  failNextMessage = false;

  async ensureConversation(input: { binding: ChatwootBinding; contactIdentifier: string; remoteContactId: string; displayName?: string; remoteThreadId: string }): Promise<ChatwootConversationRef> {
    this.ensureCalls.push({ binding: input.binding, contactIdentifier: input.contactIdentifier, remoteContactId: input.remoteContactId, remoteThreadId: input.remoteThreadId });
    const suffix = `${input.binding.chatwootAccountId}-${input.binding.chatwootInboxId}-${input.remoteThreadId}`;
    return { contactId: `contact-${suffix}`, sourceId: `source-${suffix}`, conversationId: `conversation-${suffix}` };
  }

  async createIncomingMessage(input: { binding: ChatwootBinding; conversation: ChatwootConversationRef; sourceEventId: string; text?: string; attachments: NormalizedMessage["attachments"]; normalized: NormalizedMessage }): Promise<{ messageId: string }> {
    this.messageCalls.push({ binding: input.binding, conversation: input.conversation, sourceEventId: input.sourceEventId, normalized: input.normalized });
    if (this.failNextMessage) {
      this.failNextMessage = false;
      throw new Error("CHATWOOT_TIMEOUT");
    }
    return { messageId: `msg-${input.sourceEventId}` };
  }
}

function fixture() {
  db = new Database(":memory:", { strict: true });
  runMigrations(db);
  const tenants = new SQLiteTenantRepository(db);
  const connections = new SQLiteMetaConnectionRepository(db);
  const egress = new SQLiteEgressProfileRepository(db);
  const chatwootBindings = new SQLiteChatwootBindingRepository(db);
  const conversationBindings = new SQLiteConversationBindingRepository(db);
  const processedEvents = new SQLiteProcessedEventRepository(db);
  const gateway = new FakeChatwootGateway();
  const service = new MatrixToChatwootService(tenants, connections, chatwootBindings, conversationBindings, processedEvents, gateway);

  const createRoute = (suffix: string, accountId: string, inboxId: string) => {
    const tenant = tenants.create({ slug: `tenant-${suffix}`, name: `Tenant ${suffix}` });
    const profile = egress.create({ provider: "fixture", scheme: "http", host: `proxy-${suffix}.test`, port: 8080, username: null, secretRef: null, country: null, region: null, stickySessionId: null, expectedExitIp: null, status: "healthy" });
    const connection = connections.create({ tenantId: tenant.id, matrixOwnerMxid: `@${suffix}:test`, metaAccountId: `meta-${suffix}` });
    connections.assignEgress(connection.id, profile.id);
    const binding = chatwootBindings.create({ tenantId: tenant.id, chatwootAccountId: accountId, chatwootInboxId: inboxId, apiBaseUrl: `https://chatwoot-${suffix}.test`, credentialRef: `env:CHATWOOT_${suffix.toUpperCase()}_TOKEN`, status: "active" });
    connections.assignChatwootBinding(connection.id, binding.id);
    connections.setStatus(connection.id, "active");
    return { tenant, connection: connections.findById(connection.id)!, binding };
  };

  return { tenants, connections, chatwootBindings, conversationBindings, processedEvents, gateway, service, createRoute };
}

function event(connectionId: string, overrides: Partial<MatrixInboundEvent> = {}): MatrixInboundEvent {
  return {
    connectionId,
    roomId: "!room:test",
    remoteThreadId: "thread-1",
    remoteContactId: "contact-remote",
    eventId: "$event-1",
    senderId: "remote-user-1",
    senderDisplayName: "Remote User",
    text: "hello",
    occurredAt: "2026-09-11T04:00:00.000Z",
    provenance: "meta",
    ...overrides
  };
}

describe("Phase 4 Matrix -> Chatwoot", () => {
  test("two tenants with colliding remote contact IDs remain isolated", async () => {
    const f = fixture();
    const a = f.createRoute("a", "1", "10");
    const b = f.createRoute("b", "1", "10");

    await f.service.handle(event(a.connection.id, { eventId: "$a", roomId: "!a:test", remoteThreadId: "thread-a" }));
    await f.service.handle(event(b.connection.id, { eventId: "$b", roomId: "!b:test", remoteThreadId: "thread-b" }));

    expect(f.gateway.ensureCalls).toHaveLength(2);
    expect(f.gateway.ensureCalls[0]!.contactIdentifier).not.toBe(f.gateway.ensureCalls[1]!.contactIdentifier);
    expect(f.gateway.messageCalls[0]!.normalized.tenantId).toBe(a.tenant.id);
    expect(f.gateway.messageCalls[1]!.normalized.tenantId).toBe(b.tenant.id);
    expect(f.gateway.messageCalls[0]!.binding.id).toBe(a.binding.id);
    expect(f.gateway.messageCalls[1]!.binding.id).toBe(b.binding.id);
  });

  test("duplicate Matrix event never repeats downstream side effects", async () => {
    const f = fixture();
    const route = f.createRoute("dup", "2", "20");
    const first = await f.service.handle(event(route.connection.id));
    const second = await f.service.handle(event(route.connection.id));
    expect(first.status).toBe("delivered");
    expect(second.status).toBe("duplicate");
    expect(f.gateway.ensureCalls).toHaveLength(1);
    expect(f.gateway.messageCalls).toHaveLength(1);
  });

  test("same event ID with changed payload fails closed", async () => {
    const f = fixture();
    const route = f.createRoute("conflict", "3", "30");
    await f.service.handle(event(route.connection.id));
    expect(f.service.handle(event(route.connection.id, { text: "different" }))).rejects.toThrow("EVENT_IDENTITY_CONFLICT");
    expect(f.gateway.messageCalls).toHaveLength(1);
  });

  test("existing conversation binding is reused for later events", async () => {
    const f = fixture();
    const route = f.createRoute("reuse", "4", "40");
    await f.service.handle(event(route.connection.id, { eventId: "$one" }));
    await f.service.handle(event(route.connection.id, { eventId: "$two", text: "second" }));
    expect(f.gateway.ensureCalls).toHaveLength(1);
    expect(f.gateway.messageCalls).toHaveLength(2);
    expect(f.gateway.messageCalls[0]!.conversation.conversationId).toBe(f.gateway.messageCalls[1]!.conversation.conversationId);
  });

  test("Chatwoot-originated Matrix events are suppressed explicitly", async () => {
    const f = fixture();
    const route = f.createRoute("echo", "5", "50");
    const result = await f.service.handle(event(route.connection.id, { provenance: "chatwoot" }));
    expect(result.status).toBe("ignored_echo");
    expect(f.gateway.ensureCalls).toHaveLength(0);
    expect(f.gateway.messageCalls).toHaveLength(0);
    expect(f.processedEvents.find("matrix", "$event-1")).toBeNull();
  });

  test("disabled tenant blocks routing before Chatwoot side effects", async () => {
    const f = fixture();
    const route = f.createRoute("disabled", "6", "60");
    f.tenants.setStatus(route.tenant.id, "disabled");
    expect(f.service.handle(event(route.connection.id))).rejects.toThrow("TENANT_NOT_ACTIVE");
    expect(f.gateway.ensureCalls).toHaveLength(0);
    expect(f.gateway.messageCalls).toHaveLength(0);
  });

  test("cross-tenant Chatwoot binding assignment is rejected", () => {
    const f = fixture();
    const a = f.createRoute("x-a", "7", "70");
    const b = f.createRoute("x-b", "8", "80");
    expect(() => f.connections.assignChatwootBinding(a.connection.id, b.binding.id)).toThrow("CROSS_TENANT_CHATWOOT_BINDING");
  });

  test("same remote thread cannot silently move Matrix rooms", async () => {
    const f = fixture();
    const route = f.createRoute("room", "9", "90");
    await f.service.handle(event(route.connection.id, { eventId: "$room-1", roomId: "!one:test" }));
    expect(f.service.handle(event(route.connection.id, { eventId: "$room-2", roomId: "!two:test" }))).rejects.toThrow("MATRIX_ROOM_BINDING_CONFLICT");
    expect(f.processedEvents.find("matrix", "$room-2")?.status).toBe("failed_terminal");
    expect(f.gateway.messageCalls).toHaveLength(1);
  });

  test("retryable delivery resumes safely using the same persisted binding", async () => {
    const f = fixture();
    const route = f.createRoute("retry", "10", "100");
    f.gateway.failNextMessage = true;
    const evt = event(route.connection.id, { eventId: "$retry" });
    expect(f.service.handle(evt)).rejects.toThrow("CHATWOOT_TIMEOUT");
    expect(f.processedEvents.find("matrix", "$retry")?.status).toBe("failed_retryable");
    const result = await f.service.handle(evt);
    expect(result.status).toBe("delivered");
    expect(f.gateway.ensureCalls).toHaveLength(1);
    expect(f.gateway.messageCalls).toHaveLength(2);
    expect(f.processedEvents.find("matrix", "$retry")?.status).toBe("delivered");
  });

  test("representative attachment survives normalization", async () => {
    const f = fixture();
    const route = f.createRoute("attachment", "11", "110");
    await f.service.handle(event(route.connection.id, {
      eventId: "$attachment",
      text: undefined,
      attachments: [{ kind: "image", url: "mxc://example/media", mimeType: "image/jpeg", fileName: "photo.jpg", sizeBytes: 1234 }]
    }));
    const normalized = f.gateway.messageCalls[0]!.normalized;
    expect(normalized.text).toBeUndefined();
    expect(normalized.attachments).toEqual([{ kind: "image", url: "mxc://example/media", mimeType: "image/jpeg", fileName: "photo.jpg", sizeBytes: 1234 }]);
  });

  test("contact identifier is stable but tenant and connection scoped", () => {
    const first = deterministicChatwootContactIdentifier({ tenantId: "tenant-a", connectionId: "conn-a", remoteContactId: "42" });
    expect(first).toBe(deterministicChatwootContactIdentifier({ tenantId: "tenant-a", connectionId: "conn-a", remoteContactId: "42" }));
    expect(first).not.toBe(deterministicChatwootContactIdentifier({ tenantId: "tenant-b", connectionId: "conn-a", remoteContactId: "42" }));
    expect(first).not.toBe(deterministicChatwootContactIdentifier({ tenantId: "tenant-a", connectionId: "conn-b", remoteContactId: "42" }));
  });
});
