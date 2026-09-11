import type {
  Attachment, ChatwootBinding, ChatwootBindingRepository, ConversationBindingRepository, MetaConnectionRepository,
  NormalizedMessage, ProcessedEventRepository, TenantRepository
} from "../domain/models";
import { deterministicChatwootContactIdentifier, type ChatwootGateway } from "./chatwoot-gateway";

export type MatrixInboundEvent = {
  connectionId: string;
  roomId: string;
  remoteThreadId: string;
  remoteContactId: string;
  eventId: string;
  senderId: string;
  senderDisplayName?: string;
  text?: string;
  attachments?: Attachment[];
  occurredAt: string;
  provenance: "meta" | "chatwoot";
};

export type MatrixToChatwootResult =
  | { status: "delivered"; processedEventId: string; conversationBindingId: string; chatwootMessageId: string }
  | { status: "duplicate"; processedEventId: string; priorStatus: string }
  | { status: "ignored_echo" };

function canonicalEventPayload(event: MatrixInboundEvent): string {
  return JSON.stringify({
    connectionId: event.connectionId,
    roomId: event.roomId,
    remoteThreadId: event.remoteThreadId,
    remoteContactId: event.remoteContactId,
    eventId: event.eventId,
    senderId: event.senderId,
    senderDisplayName: event.senderDisplayName ?? null,
    text: event.text ?? null,
    attachments: event.attachments ?? [],
    occurredAt: event.occurredAt,
    provenance: event.provenance
  });
}

function payloadHash(event: MatrixInboundEvent): string {
  return new Bun.CryptoHasher("sha256").update(canonicalEventPayload(event)).digest("hex");
}

export class MatrixToChatwootService {
  constructor(
    private readonly tenants: TenantRepository,
    private readonly connections: MetaConnectionRepository,
    private readonly chatwootBindings: ChatwootBindingRepository,
    private readonly conversationBindings: ConversationBindingRepository,
    private readonly processedEvents: ProcessedEventRepository,
    private readonly chatwoot: ChatwootGateway
  ) {}

  private historicalBinding(tenantId: string, accountId: string, inboxId: string): ChatwootBinding {
    const matches = this.chatwootBindings.listForTenant(tenantId).filter((binding) =>
      binding.chatwootAccountId === accountId && binding.chatwootInboxId === inboxId
    );
    if (matches.length !== 1) throw new Error(matches.length === 0 ? "CHATWOOT_BINDING_NOT_ACTIVE" : "CHATWOOT_ROUTE_AMBIGUOUS");
    const binding = matches[0]!;
    if (binding.status !== "active") throw new Error("CHATWOOT_BINDING_NOT_ACTIVE");
    return binding;
  }

  private currentBinding(connection: { chatwootBindingId: string | null; tenantId: string }): ChatwootBinding {
    if (!connection.chatwootBindingId) throw new Error("CHATWOOT_BINDING_REQUIRED");
    const binding = this.chatwootBindings.findById(connection.chatwootBindingId);
    if (!binding || binding.status !== "active") throw new Error("CHATWOOT_BINDING_NOT_ACTIVE");
    if (binding.tenantId !== connection.tenantId) throw new Error("CROSS_TENANT_CHATWOOT_BINDING");
    return binding;
  }

  async handle(event: MatrixInboundEvent): Promise<MatrixToChatwootResult> {
    if (event.provenance === "chatwoot") return { status: "ignored_echo" };

    const connection = this.connections.findById(event.connectionId);
    if (!connection) throw new Error("CONNECTION_NOT_FOUND");
    const tenant = this.tenants.findById(connection.tenantId);
    if (!tenant || tenant.status !== "active") throw new Error("TENANT_NOT_ACTIVE");
    if (connection.status !== "active") throw new Error("CONNECTION_NOT_ACTIVE");

    const claim = this.processedEvents.claim({
      source: "matrix",
      sourceEventId: event.eventId,
      metaConnectionId: connection.id,
      payloadHash: payloadHash(event)
    });
    const retrying = !claim.claimed && claim.event.status === "failed_retryable";
    if (!claim.claimed && !retrying) {
      return { status: "duplicate", processedEventId: claim.event.id, priorStatus: claim.event.status };
    }

    this.processedEvents.setStatus(claim.event.id, "processing");

    try {
      let conversationBinding = this.conversationBindings.findByRemoteThread(connection.id, event.remoteThreadId);
      if (conversationBinding && conversationBinding.tenantId !== connection.tenantId) throw new Error("CROSS_TENANT_CONVERSATION_BINDING");
      if (conversationBinding && conversationBinding.matrixRoomId !== event.roomId) throw new Error("MATRIX_ROOM_BINDING_CONFLICT");

      let deliveryBinding: ChatwootBinding;
      let conversationRef;
      if (!conversationBinding) {
        const currentChatwootBinding = this.currentBinding(connection);
        deliveryBinding = currentChatwootBinding;
        conversationRef = await this.chatwoot.ensureConversation({
          binding: currentChatwootBinding,
          contactIdentifier: deterministicChatwootContactIdentifier({ tenantId: tenant.id, connectionId: connection.id, remoteContactId: event.remoteContactId }),
          remoteContactId: event.remoteContactId,
          ...(event.senderDisplayName ? { displayName: event.senderDisplayName } : {}),
          remoteThreadId: event.remoteThreadId
        });
        const racedBinding = this.conversationBindings.findByRemoteThread(connection.id, event.remoteThreadId);
        conversationBinding = racedBinding ?? this.conversationBindings.create({
          tenantId: tenant.id,
          metaConnectionId: connection.id,
          matrixRoomId: event.roomId,
          remoteThreadId: event.remoteThreadId,
          remoteContactId: event.remoteContactId,
          chatwootAccountId: currentChatwootBinding.chatwootAccountId,
          chatwootInboxId: currentChatwootBinding.chatwootInboxId,
          chatwootContactId: conversationRef.contactId,
          chatwootSourceId: conversationRef.sourceId,
          chatwootConversationId: conversationRef.conversationId
        });
        if (conversationBinding.matrixRoomId !== event.roomId) throw new Error("MATRIX_ROOM_BINDING_CONFLICT");
        if (!conversationBinding.chatwootContactId || !conversationBinding.chatwootSourceId) throw new Error("INCOMPLETE_CONVERSATION_BINDING");
        if (racedBinding) {
          deliveryBinding = this.historicalBinding(tenant.id, conversationBinding.chatwootAccountId, conversationBinding.chatwootInboxId);
        }
        conversationRef = {
          contactId: conversationBinding.chatwootContactId,
          sourceId: conversationBinding.chatwootSourceId,
          conversationId: conversationBinding.chatwootConversationId
        };
      } else {
        deliveryBinding = this.historicalBinding(tenant.id, conversationBinding.chatwootAccountId, conversationBinding.chatwootInboxId);
        if (!conversationBinding.chatwootContactId || !conversationBinding.chatwootSourceId) throw new Error("INCOMPLETE_CONVERSATION_BINDING");
        conversationRef = {
          contactId: conversationBinding.chatwootContactId,
          sourceId: conversationBinding.chatwootSourceId,
          conversationId: conversationBinding.chatwootConversationId
        };
      }

      const normalized: NormalizedMessage = {
        tenantId: tenant.id,
        connectionId: connection.id,
        conversationExternalId: event.remoteThreadId,
        messageExternalId: event.eventId,
        senderExternalId: event.senderId,
        ...(event.senderDisplayName ? { senderDisplayName: event.senderDisplayName } : {}),
        direction: "inbound",
        ...(event.text ? { text: event.text } : {}),
        attachments: event.attachments ?? [],
        occurredAt: event.occurredAt,
        source: "matrix",
        sourceEventId: event.eventId
      };
      if (!normalized.text && normalized.attachments.length === 0) throw new Error("EMPTY_MESSAGE");

      const delivered = await this.chatwoot.createIncomingMessage({
        binding: deliveryBinding,
        conversation: conversationRef,
        sourceEventId: event.eventId,
        ...(event.text ? { text: event.text } : {}),
        attachments: normalized.attachments,
        normalized
      });
      this.processedEvents.setStatus(claim.event.id, "delivered");
      return { status: "delivered", processedEventId: claim.event.id, conversationBindingId: conversationBinding.id, chatwootMessageId: delivered.messageId };
    } catch (error) {
      const message = error instanceof Error ? error.message : "UNKNOWN_ERROR";
      const terminal = [
        "CHATWOOT_BINDING_REQUIRED", "CROSS_TENANT_CHATWOOT_BINDING", "CROSS_TENANT_CONVERSATION_BINDING",
        "CHATWOOT_ROUTE_MISMATCH", "CHATWOOT_ROUTE_AMBIGUOUS", "CHATWOOT_BINDING_NOT_ACTIVE",
        "MATRIX_ROOM_BINDING_CONFLICT", "INCOMPLETE_CONVERSATION_BINDING", "EMPTY_MESSAGE"
      ].includes(message);
      this.processedEvents.setStatus(claim.event.id, terminal ? "failed_terminal" : "failed_retryable", message);
      throw error;
    }
  }
}
