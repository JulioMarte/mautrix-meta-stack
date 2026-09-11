import type { ChatwootBinding, SecretProvider } from "../domain/models";
import type { ChatwootConversationRef, ChatwootGateway, ChatwootIncomingMessageResult } from "./chatwoot-gateway";
import type { MatrixMediaDownloader } from "./matrix-media-downloader";

const THREAD_ATTRIBUTE = "mautrix_meta_remote_thread_id";
const EVENT_ATTRIBUTE = "mautrix_meta_source_event_id";
const MAX_ATTACHMENTS_PER_MESSAGE = 15;

type FetchLike = (input: string | URL | Request, init?: RequestInit) => Promise<Response>;

type ContactRecord = {
  id: number | string;
  identifier?: string | null;
  contact_inboxes?: Array<{ source_id?: string | null; inbox?: { id?: number | string } }>;
};

type ConversationRecord = {
  id: number | string;
  inbox_id?: number | string;
  custom_attributes?: Record<string, unknown>;
};

type MessageRecord = {
  id: number | string;
  content_attributes?: Record<string, unknown>;
};

function validatedBaseUrl(raw: string): URL {
  let url: URL;
  try { url = new URL(raw); } catch { throw new Error("CHATWOOT_BASE_URL_INVALID"); }
  if (!new Set(["http:", "https:"]).has(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error("CHATWOOT_BASE_URL_INVALID");
  return url;
}

function appendPath(base: URL, path: string): URL {
  const url = new URL(base.toString());
  const prefix = url.pathname.replace(/\/+$/, "");
  url.pathname = `${prefix}${path.startsWith("/") ? path : `/${path}`}`;
  url.search = "";
  url.hash = "";
  return url;
}

function numericIdentifier(value: string, code: string): number {
  if (!/^[1-9][0-9]*$/.test(value)) throw new Error(code);
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed)) throw new Error(code);
  return parsed;
}

function asObject(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

function asArray(value: unknown): unknown[] { return Array.isArray(value) ? value : []; }

export class HttpChatwootGateway implements ChatwootGateway {
  constructor(
    private readonly secrets: SecretProvider,
    private readonly fetchImpl: FetchLike = fetch,
    private readonly timeoutMs = 8_000,
    private readonly mediaDownloader?: MatrixMediaDownloader
  ) {}

  private async request(binding: ChatwootBinding, path: string, init: RequestInit = {}): Promise<Response> {
    const token = this.secrets.resolve(binding.credentialRef);
    if (!token) throw new Error("CHATWOOT_CREDENTIAL_MISSING");
    const base = validatedBaseUrl(binding.apiBaseUrl);
    const url = appendPath(base, path);
    if (url.origin !== base.origin) throw new Error("CHATWOOT_REQUEST_ORIGIN_MISMATCH");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const headers = new Headers(init.headers);
      headers.set("api_access_token", token);
      if (init.body && !(init.body instanceof FormData) && !headers.has("content-type")) headers.set("content-type", "application/json");
      return await this.fetchImpl(url, { ...init, headers, signal: controller.signal, redirect: "error" });
    } catch (error) {
      if (controller.signal.aborted) throw new Error("CHATWOOT_TIMEOUT");
      throw new Error("CHATWOOT_NETWORK_ERROR", { cause: error });
    } finally {
      clearTimeout(timeout);
    }
  }

  private async json(binding: ChatwootBinding, path: string, init: RequestInit = {}, allowed = [200]): Promise<unknown> {
    const response = await this.request(binding, path, init);
    if (!allowed.includes(response.status)) {
      await response.body?.cancel().catch(() => undefined);
      throw new Error(`CHATWOOT_HTTP_${response.status}`);
    }
    try { return await response.json(); } catch { throw new Error("CHATWOOT_RESPONSE_INVALID"); }
  }

  private accountAndInbox(binding: ChatwootBinding): { accountId: number; inboxId: number } {
    return {
      accountId: numericIdentifier(binding.chatwootAccountId, "CHATWOOT_ACCOUNT_ID_INVALID"),
      inboxId: numericIdentifier(binding.chatwootInboxId, "CHATWOOT_INBOX_ID_INVALID")
    };
  }

  private async findContact(binding: ChatwootBinding, identifier: string): Promise<ContactRecord | null> {
    const { accountId } = this.accountAndInbox(binding);
    const body = await this.json(binding, `/api/v1/accounts/${accountId}/contacts/filter`, {
      method: "POST",
      body: JSON.stringify({ payload: [{ attribute_key: "identifier", filter_operator: "equal_to", values: [identifier], query_operator: null }] })
    });
    const matches = asArray(asObject(body).payload).map(asObject).filter((row) => row.identifier === identifier) as ContactRecord[];
    if (matches.length > 1) throw new Error("CHATWOOT_CONTACT_AMBIGUOUS");
    return matches[0] ?? null;
  }

  private sourceForInbox(contact: ContactRecord, inboxId: number): string | null {
    const matches = (contact.contact_inboxes ?? []).filter((ci) => Number(ci.inbox?.id) === inboxId && typeof ci.source_id === "string" && ci.source_id.length > 0);
    if (matches.length > 1) throw new Error("CHATWOOT_CONTACT_INBOX_AMBIGUOUS");
    return matches[0]?.source_id ?? null;
  }

  private async ensureContact(binding: ChatwootBinding, identifier: string, displayName?: string): Promise<{ contactId: string; sourceId: string }> {
    const { accountId, inboxId } = this.accountAndInbox(binding);
    let contact = await this.findContact(binding, identifier);
    if (!contact) {
      try {
        const created = asObject(await this.json(binding, `/api/v1/accounts/${accountId}/contacts`, {
          method: "POST",
          body: JSON.stringify({ inbox_id: inboxId, identifier, name: displayName ?? identifier })
        }));
        const payload = asArray(created.payload).map(asObject);
        const record = payload.find((row) => String(row.identifier ?? "") === identifier) ?? payload[0];
        if (!record) throw new Error("CHATWOOT_CONTACT_RESPONSE_INVALID");
        contact = record as ContactRecord;
      } catch (error) {
        if (error instanceof Error && error.message === "CHATWOOT_CONTACT_RESPONSE_INVALID") throw error;
        contact = await this.findContact(binding, identifier);
        if (!contact) throw error;
      }
    }
    const contactId = String(contact.id ?? "");
    if (!contactId) throw new Error("CHATWOOT_CONTACT_RESPONSE_INVALID");
    let sourceId = this.sourceForInbox(contact, inboxId);
    if (!sourceId) {
      const requestedSourceId = identifier;
      try {
        const result = asObject(await this.json(binding, `/api/v1/accounts/${accountId}/contacts/${encodeURIComponent(contactId)}/contact_inboxes`, {
          method: "POST",
          body: JSON.stringify({ inbox_id: inboxId, source_id: requestedSourceId })
        }));
        sourceId = typeof result.source_id === "string" ? result.source_id : null;
      } catch (error) {
        const refreshed = await this.findContact(binding, identifier);
        sourceId = refreshed ? this.sourceForInbox(refreshed, inboxId) : null;
        if (!sourceId) throw error;
      }
    }
    if (!sourceId) throw new Error("CHATWOOT_SOURCE_ID_MISSING");
    return { contactId, sourceId };
  }

  private async findConversation(binding: ChatwootBinding, contactId: string, remoteThreadId: string): Promise<ConversationRecord | null> {
    const { accountId, inboxId } = this.accountAndInbox(binding);
    const body = asObject(await this.json(binding, `/api/v1/accounts/${accountId}/contacts/${encodeURIComponent(contactId)}/conversations`));
    const matches = asArray(body.payload).map(asObject).filter((row) => Number(row.inbox_id) === inboxId && asObject(row.custom_attributes)[THREAD_ATTRIBUTE] === remoteThreadId) as ConversationRecord[];
    if (matches.length > 1) throw new Error("CHATWOOT_CONVERSATION_AMBIGUOUS");
    return matches[0] ?? null;
  }

  async ensureConversation(input: {
    binding: ChatwootBinding;
    contactIdentifier: string;
    remoteContactId: string;
    displayName?: string;
    remoteThreadId: string;
  }): Promise<ChatwootConversationRef> {
    const { accountId, inboxId } = this.accountAndInbox(input.binding);
    const contact = await this.ensureContact(input.binding, input.contactIdentifier, input.displayName);
    let conversation = await this.findConversation(input.binding, contact.contactId, input.remoteThreadId);
    if (!conversation) {
      try {
        conversation = asObject(await this.json(input.binding, `/api/v1/accounts/${accountId}/conversations`, {
          method: "POST",
          body: JSON.stringify({
            source_id: contact.sourceId,
            inbox_id: inboxId,
            contact_id: numericIdentifier(contact.contactId, "CHATWOOT_CONTACT_ID_INVALID"),
            status: "open",
            custom_attributes: { [THREAD_ATTRIBUTE]: input.remoteThreadId }
          })
        })) as ConversationRecord;
      } catch (error) {
        conversation = await this.findConversation(input.binding, contact.contactId, input.remoteThreadId);
        if (!conversation) throw error;
      }
    }
    const conversationId = String(conversation.id ?? "");
    if (!conversationId) throw new Error("CHATWOOT_CONVERSATION_RESPONSE_INVALID");
    return { contactId: contact.contactId, sourceId: contact.sourceId, conversationId };
  }

  private async findMessage(binding: ChatwootBinding, conversationId: string, sourceEventId: string): Promise<MessageRecord | null> {
    const { accountId } = this.accountAndInbox(binding);
    const body = asObject(await this.json(binding, `/api/v1/accounts/${accountId}/conversations/${encodeURIComponent(conversationId)}/messages`));
    const matches = asArray(body.payload).map(asObject).filter((row) => asObject(row.content_attributes)[EVENT_ATTRIBUTE] === sourceEventId) as MessageRecord[];
    if (matches.length > 1) throw new Error("CHATWOOT_MESSAGE_AMBIGUOUS");
    return matches[0] ?? null;
  }

  private async multipartMessage(input: Parameters<ChatwootGateway["createIncomingMessage"]>[0]): Promise<FormData> {
    if (!this.mediaDownloader) throw new Error("MATRIX_MEDIA_DOWNLOADER_NOT_CONFIGURED");
    if (input.attachments.length > MAX_ATTACHMENTS_PER_MESSAGE) throw new Error("CHATWOOT_ATTACHMENT_LIMIT_EXCEEDED");
    const form = new FormData();
    form.set("content", input.text ?? "");
    form.set("message_type", "incoming");
    form.set("private", "false");
    form.set("content_type", "text");
    form.set(`content_attributes[${EVENT_ATTRIBUTE}]`, input.sourceEventId);
    for (const attachment of input.attachments) {
      const downloaded = await this.mediaDownloader.download(attachment);
      form.append("attachments[]", downloaded.blob, downloaded.fileName);
    }
    return form;
  }

  async createIncomingMessage(input: Parameters<ChatwootGateway["createIncomingMessage"]>[0]): Promise<ChatwootIncomingMessageResult> {
    const existing = await this.findMessage(input.binding, input.conversation.conversationId, input.sourceEventId);
    if (existing) return { messageId: String(existing.id) };
    const { accountId } = this.accountAndInbox(input.binding);
    const path = `/api/v1/accounts/${accountId}/conversations/${encodeURIComponent(input.conversation.conversationId)}/messages`;
    try {
      const init: RequestInit = input.attachments.length > 0
        ? { method: "POST", body: await this.multipartMessage(input) }
        : {
            method: "POST",
            body: JSON.stringify({
              content: input.text ?? "",
              message_type: "incoming",
              private: false,
              content_type: "text",
              content_attributes: { [EVENT_ATTRIBUTE]: input.sourceEventId }
            })
          };
      const body = asObject(await this.json(input.binding, path, init));
      if (body.id == null) throw new Error("CHATWOOT_MESSAGE_RESPONSE_INVALID");
      return { messageId: String(body.id) };
    } catch (error) {
      const reconciled = await this.findMessage(input.binding, input.conversation.conversationId, input.sourceEventId);
      if (reconciled) return { messageId: String(reconciled.id) };
      throw error;
    }
  }
}
