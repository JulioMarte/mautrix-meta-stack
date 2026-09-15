import type { Attachment, ChatwootBinding, NormalizedMessage } from "../domain/models";

export type ChatwootConversationRef = {
  contactId: string;
  sourceId: string;
  conversationId: string;
};

export type ChatwootIncomingMessageResult = {
  messageId: string;
};

/**
 * Network adapter contract for Chatwoot.
 *
 * ensureConversation MUST be retry-safe for the deterministic contact key supplied
 * by the caller. createIncomingMessage MUST reconcile an ambiguous prior attempt
 * using sourceEventId before creating another customer-visible message.
 */
export interface ChatwootGateway {
  ensureConversation(input: {
    binding: ChatwootBinding;
    contactIdentifier: string;
    remoteContactId: string;
    displayName?: string;
    remoteThreadId: string;
  }): Promise<ChatwootConversationRef>;

  createIncomingMessage(input: {
    binding: ChatwootBinding;
    conversation: ChatwootConversationRef;
    sourceEventId: string;
    text?: string;
    attachments: Attachment[];
    normalized: NormalizedMessage;
  }): Promise<ChatwootIncomingMessageResult>;
}

export function deterministicChatwootContactIdentifier(input: {
  tenantId: string;
  connectionId: string;
  remoteContactId: string;
}): string {
  const bytes = new TextEncoder().encode(`${input.tenantId}\u0000${input.connectionId}\u0000${input.remoteContactId}`);
  const hash = new Bun.CryptoHasher("sha256").update(bytes).digest("hex");
  return `meta_${hash.slice(0, 40)}`;
}
