type Contact = { id: number; accountId: number; inboxId: number; identifier: string; name: string; sourceId: string };
type Conversation = { id: number; accountId: number; inboxId: number; contactId: number; sourceId: string; threadId: string };
type StoredAttachment = { name: string; type: string; size: number; content: string };
type Message = { id: number; accountId: number; inboxId: number; conversationId: number; content: string; sourceEventId: string; attachments: StoredAttachment[]; voiceMessage: boolean };

const expectedToken = process.env.CHATWOOT_CI_TOKEN ?? "";
const contacts: Contact[] = [];
const conversations: Conversation[] = [];
const messages: Message[] = [];
let nextContact = 1;
let nextConversation = 1;
let nextMessage = 1;
let failAfterNextMessageCreate = false;

function json(value: unknown, status = 200) { return Response.json(value, { status }); }
function authorized(req: Request) { return expectedToken.length > 0 && req.headers.get("api_access_token") === expectedToken; }
function pathParts(url: URL) { return url.pathname.split("/").filter(Boolean); }
async function body(req: Request) { try { return await req.json() as Record<string, unknown>; } catch { return {}; } }

async function messageBody(req: Request): Promise<{ content: string; sourceEventId: string; attachments: StoredAttachment[]; voiceMessage: boolean }> {
  const contentType = req.headers.get("content-type") ?? "";
  if (!contentType.startsWith("multipart/form-data")) {
    const input = await body(req);
    return {
      content: String(input.content ?? ""),
      sourceEventId: String((input.content_attributes as Record<string, unknown> | undefined)?.mautrix_meta_source_event_id ?? ""),
      attachments: [],
      voiceMessage: input.is_voice_message === true || input.is_voice_message === "true"
    };
  }
  const form = await req.formData();
  const attachments: StoredAttachment[] = [];
  for (const value of form.getAll("attachments[]")) {
    if (!(value instanceof File)) continue;
    attachments.push({ name: value.name, type: value.type, size: value.size, content: await value.text() });
  }
  return {
    content: String(form.get("content") ?? ""),
    sourceEventId: String(form.get("content_attributes[mautrix_meta_source_event_id]") ?? ""),
    attachments,
    voiceMessage: String(form.get("is_voice_message") ?? "false") === "true"
  };
}

Bun.serve({
  port: 8080,
  async fetch(req) {
    const url = new URL(req.url);
    if (url.pathname === "/health") return json({ status: "ok" });
    if (url.pathname === "/_test/state") return json({ contacts, conversations, messages });
    if (url.pathname === "/_test/fail-after-next-message-create" && req.method === "POST") { failAfterNextMessageCreate = true; return json({ ok: true }); }
    if (!authorized(req)) return json({ error: "unauthorized" }, 401);

    if (url.pathname === "/files/agent.pdf" && req.method === "GET") {
      return new Response("phase5-agent-pdf", { status: 200, headers: { "content-type": "application/pdf" } });
    }
    if (url.pathname === "/files/agent.ogg" && req.method === "GET") {
      return new Response("phase5-agent-voice", { status: 200, headers: { "content-type": "audio/ogg" } });
    }

    const p = pathParts(url);
    const accountIndex = p.indexOf("accounts");
    if (accountIndex < 0) return json({ error: "not found" }, 404);
    const accountId = Number(p[accountIndex + 1]);
    const rest = p.slice(accountIndex + 2);

    if (rest.join("/") === "contacts/filter" && req.method === "POST") {
      const input = await body(req);
      const identifier = String((((input.payload as unknown[])?.[0] as Record<string, unknown>)?.values as unknown[])?.[0] ?? "");
      return json({ payload: contacts.filter((c) => c.accountId === accountId && c.identifier === identifier).map((c) => ({ id: c.id, identifier: c.identifier, name: c.name, contact_inboxes: [{ source_id: c.sourceId, inbox: { id: c.inboxId } }] })) });
    }

    if (rest.length === 1 && rest[0] === "contacts" && req.method === "POST") {
      const input = await body(req);
      const identifier = String(input.identifier ?? "");
      const inboxId = Number(input.inbox_id);
      const existing = contacts.find((c) => c.accountId === accountId && c.identifier === identifier);
      const contact = existing ?? { id: nextContact++, accountId, inboxId, identifier, name: String(input.name ?? identifier), sourceId: identifier };
      if (!existing) contacts.push(contact);
      return json({ payload: [{ id: contact.id, identifier: contact.identifier, name: contact.name, contact_inboxes: [{ source_id: contact.sourceId, inbox: { id: contact.inboxId } }] }] });
    }

    if (rest[0] === "contacts" && rest[2] === "contact_inboxes" && req.method === "POST") {
      const contact = contacts.find((c) => c.accountId === accountId && c.id === Number(rest[1]));
      if (!contact) return json({}, 404);
      const input = await body(req);
      contact.inboxId = Number(input.inbox_id);
      contact.sourceId = String(input.source_id ?? contact.identifier);
      return json({ source_id: contact.sourceId, inbox: { id: contact.inboxId } });
    }

    if (rest[0] === "contacts" && rest[2] === "conversations" && req.method === "GET") {
      const contactId = Number(rest[1]);
      return json({ payload: conversations.filter((c) => c.accountId === accountId && c.contactId === contactId).map((c) => ({ id: c.id, inbox_id: c.inboxId, custom_attributes: { mautrix_meta_remote_thread_id: c.threadId } })) });
    }

    if (rest.length === 1 && rest[0] === "conversations" && req.method === "POST") {
      const input = await body(req);
      const threadId = String((input.custom_attributes as Record<string, unknown> | undefined)?.mautrix_meta_remote_thread_id ?? "");
      const conversation: Conversation = { id: nextConversation++, accountId, inboxId: Number(input.inbox_id), contactId: Number(input.contact_id), sourceId: String(input.source_id), threadId };
      conversations.push(conversation);
      return json({ id: conversation.id, inbox_id: conversation.inboxId, custom_attributes: { mautrix_meta_remote_thread_id: threadId } });
    }

    if (rest[0] === "conversations" && rest[2] === "messages" && req.method === "GET") {
      const conversationId = Number(rest[1]);
      return json({ payload: messages.filter((m) => m.accountId === accountId && m.conversationId === conversationId).map((m) => ({ id: m.id, content: m.content, inbox_id: m.inboxId, content_attributes: { mautrix_meta_source_event_id: m.sourceEventId }, attachments: m.attachments, is_voice_message: m.voiceMessage })) });
    }

    if (rest[0] === "conversations" && rest[2] === "messages" && req.method === "POST") {
      const conversationId = Number(rest[1]);
      const conversation = conversations.find((c) => c.accountId === accountId && c.id === conversationId);
      if (!conversation) return json({}, 404);
      const input = await messageBody(req);
      const existing = messages.find((m) => m.accountId === accountId && m.conversationId === conversationId && m.sourceEventId === input.sourceEventId);
      const message = existing ?? { id: nextMessage++, accountId, inboxId: conversation.inboxId, conversationId, content: input.content, sourceEventId: input.sourceEventId, attachments: input.attachments, voiceMessage: input.voiceMessage };
      if (!existing) messages.push(message);
      if (failAfterNextMessageCreate) { failAfterNextMessageCreate = false; return json({ error: "injected post-commit failure" }, 500); }
      return json({ id: message.id, content: message.content, inbox_id: message.inboxId, conversation_id: message.conversationId, content_attributes: { mautrix_meta_source_event_id: message.sourceEventId }, attachments: message.attachments, is_voice_message: message.voiceMessage });
    }

    return json({ error: "not found" }, 404);
  }
});
