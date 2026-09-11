import type {
  ChatwootBinding, ChatwootBindingRepository, ConversationBindingRepository, MetaConnectionRepository,
  ProcessedEventRepository, TenantRepository
} from "../domain/models";
import { ChatwootAttachmentDownloader, type ChatwootWebhookAttachment } from "./chatwoot-attachment-downloader";
import type { MatrixGateway } from "./matrix-gateway";

export type ChatwootOutboundEvent = {
  messageId: string;
  accountId: string;
  inboxId: string;
  conversationId: string;
  senderId: string;
  senderName?: string;
  text?: string;
  attachments: ChatwootWebhookAttachment[];
  occurredAt: string;
};

export type ChatwootToMatrixResult =
  | { status: "delivered"; processedEventId: string; matrixEventIds: string[] }
  | { status: "duplicate"; processedEventId: string; priorStatus: string };

function canonical(input: ChatwootOutboundEvent): string {
  return JSON.stringify({
    messageId: input.messageId,
    accountId: input.accountId,
    inboxId: input.inboxId,
    conversationId: input.conversationId,
    senderId: input.senderId,
    senderName: input.senderName ?? null,
    text: input.text ?? null,
    attachments: input.attachments,
    occurredAt: input.occurredAt
  });
}

function hash(input: string): string { return new Bun.CryptoHasher("sha256").update(input).digest("hex"); }

export class ChatwootToMatrixService {
  constructor(
    private readonly tenants: TenantRepository,
    private readonly connections: MetaConnectionRepository,
    private readonly chatwootBindings: ChatwootBindingRepository,
    private readonly conversationBindings: ConversationBindingRepository,
    private readonly processedEvents: ProcessedEventRepository,
    private readonly downloader: ChatwootAttachmentDownloader,
    private readonly matrix: MatrixGateway
  ) {}

  async handle(binding: ChatwootBinding, event: ChatwootOutboundEvent): Promise<ChatwootToMatrixResult> {
    if (binding.status !== "active") throw new Error("CHATWOOT_BINDING_NOT_ACTIVE");
    if (event.accountId !== binding.chatwootAccountId || event.inboxId !== binding.chatwootInboxId) throw new Error("CHATWOOT_ROUTE_MISMATCH");
    const tenant = this.tenants.findById(binding.tenantId);
    if (!tenant || tenant.status !== "active") throw new Error("TENANT_NOT_ACTIVE");
    const conversation = this.conversationBindings.findByChatwootConversation({
      tenantId: binding.tenantId,
      accountId: event.accountId,
      inboxId: event.inboxId,
      conversationId: event.conversationId
    });
    if (!conversation) throw new Error("CONVERSATION_BINDING_NOT_FOUND");
    const connection = this.connections.findById(conversation.metaConnectionId);
    if (!connection || connection.tenantId !== binding.tenantId) throw new Error("CONNECTION_NOT_FOUND");
    if (connection.status !== "active") throw new Error("CONNECTION_NOT_ACTIVE");
    if (conversation.chatwootAccountId !== binding.chatwootAccountId || conversation.chatwootInboxId !== binding.chatwootInboxId) throw new Error("CHATWOOT_ROUTE_MISMATCH");

    const sourceEventId = `${binding.id}:${event.messageId}`;
    const claim = this.processedEvents.claim({ source: "chatwoot", sourceEventId, metaConnectionId: connection.id, payloadHash: hash(canonical(event)) });
    const retrying = !claim.claimed && claim.event.status === "failed_retryable";
    if (!claim.claimed && !retrying) return { status: "duplicate", processedEventId: claim.event.id, priorStatus: claim.event.status };
    this.processedEvents.setStatus(claim.event.id, "processing");

    try {
      const attachments = [];
      for (const attachment of event.attachments) attachments.push(await this.downloader.download(binding, attachment));
      if (!event.text && attachments.length === 0) throw new Error("EMPTY_MESSAGE");
      const transactionBase = `cw-${hash(sourceEventId).slice(0, 32)}`;
      const sent = await this.matrix.send({
        roomId: conversation.matrixRoomId,
        transactionBase,
        sourceEventId,
        ...(event.text ? { text: event.text } : {}),
        attachments
      });
      this.processedEvents.setStatus(claim.event.id, "delivered");
      return { status: "delivered", processedEventId: claim.event.id, matrixEventIds: sent.eventIds };
    } catch (error) {
      const code = error instanceof Error ? error.message : "UNKNOWN_ERROR";
      const terminal = new Set([
        "CHATWOOT_ATTACHMENT_URL_INVALID", "CHATWOOT_ATTACHMENT_ORIGIN_MISMATCH", "CHATWOOT_ATTACHMENT_REDIRECT_INVALID",
        "CHATWOOT_ATTACHMENT_REDIRECT_INSECURE", "CHATWOOT_ATTACHMENT_TOO_LARGE", "MATRIX_ATTACHMENT_BYTES_REQUIRED", "EMPTY_MESSAGE"
      ]).has(code);
      this.processedEvents.setStatus(claim.event.id, terminal ? "failed_terminal" : "failed_retryable", code);
      throw error;
    }
  }
}
