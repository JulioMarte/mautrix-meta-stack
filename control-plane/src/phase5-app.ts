import { timingSafeEqual } from "node:crypto";
import type { Database } from "bun:sqlite";
import { Elysia } from "elysia";
import {
  SQLiteChatwootBindingRepository, SQLiteConversationBindingRepository, SQLiteMetaConnectionRepository,
  SQLiteProcessedEventRepository, SQLiteTenantRepository
} from "./persistence/sqlite-repositories";
import { SQLiteChatwootWebhookConfigRepository, SQLitePhase5LookupRepository } from "./persistence/phase5-repositories";
import {
  ChatwootEnvironmentSecretProvider, ChatwootWebhookEnvironmentSecretProvider,
  isSupportedChatwootWebhookSecretRef
} from "./security/secrets";
import { ChatwootAttachmentDownloader, type ChatwootWebhookAttachment } from "./services/chatwoot-attachment-downloader";
import { chatwootWebhookHeaders, verifyChatwootWebhook } from "./services/chatwoot-webhook-auth";
import { ChatwootToMatrixService, type ChatwootOutboundEvent } from "./services/chatwoot-to-matrix";
import { HttpMatrixGateway, type MatrixGateway } from "./services/matrix-gateway";

function bearer(request: Request): string | null {
  const value = request.headers.get("authorization");
  return value?.startsWith("Bearer ") ? value.slice(7) : null;
}
function tokenMatches(actual: string | null, expected: string): boolean {
  if (!actual || expected.length < 16) return false;
  const a = Buffer.from(actual); const b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}
function error(set: { status?: number | string }, status: number, code: string, message: string) {
  set.status = status; return { error: { code, message } };
}
function object(value: unknown): Record<string, unknown> { return value && typeof value === "object" ? value as Record<string, unknown> : {}; }
function positiveId(value: unknown): string | null {
  const text = typeof value === "number" ? String(value) : typeof value === "string" ? value : "";
  return /^[1-9][0-9]*$/.test(text) ? text : null;
}
function attachmentKind(value: unknown): ChatwootWebhookAttachment["kind"] | null {
  return value === "image" || value === "video" || value === "audio" || value === "file" ? value : value === "unknown" ? "unknown" : null;
}

function parseChatwootEvent(rawBody: string): { ignored?: string; event?: ChatwootOutboundEvent } {
  let body: Record<string, unknown>;
  try { body = JSON.parse(rawBody) as Record<string, unknown>; } catch { throw new Error("CHATWOOT_WEBHOOK_JSON_INVALID"); }
  if (body.event !== "message_created") return { ignored: "unsupported_event" };
  if (body.message_type !== "outgoing") return { ignored: "not_outgoing" };
  if (body.private === true) return { ignored: "private_message" };
  const sender = object(body.sender);
  if (sender.type !== "user") return { ignored: "not_human_agent" };
  const accountId = positiveId(object(body.account).id);
  const inboxId = positiveId(object(body.inbox).id);
  const conversationId = positiveId(object(body.conversation).id);
  const messageId = positiveId(body.id);
  const senderId = positiveId(sender.id);
  if (!accountId || !inboxId || !conversationId || !messageId || !senderId) throw new Error("CHATWOOT_WEBHOOK_ROUTE_INVALID");
  const text = typeof body.content === "string" && body.content.length > 0 ? body.content : undefined;
  const attachments: ChatwootWebhookAttachment[] = [];
  const rawAttachments = body.attachments ?? [];
  if (!Array.isArray(rawAttachments)) throw new Error("CHATWOOT_WEBHOOK_ATTACHMENTS_INVALID");
  for (const raw of rawAttachments) {
    const item = object(raw);
    const id = positiveId(item.id);
    const kind = attachmentKind(item.file_type);
    if (!id || !kind || typeof item.data_url !== "string" || !item.data_url) throw new Error("CHATWOOT_WEBHOOK_ATTACHMENT_INVALID");
    const extension = typeof item.extension === "string" && /^[a-zA-Z0-9]{1,12}$/.test(item.extension) ? `.${item.extension.toLowerCase()}` : "";
    const sizeBytes = typeof item.file_size === "number" && Number.isSafeInteger(item.file_size) && item.file_size >= 0 ? item.file_size : undefined;
    attachments.push({
      id,
      kind,
      dataUrl: item.data_url,
      ...(typeof item.content_type === "string" && item.content_type ? { mimeType: item.content_type } : {}),
      fileName: `attachment-${id}${extension}`,
      ...(sizeBytes != null ? { sizeBytes } : {})
    });
  }
  let occurredAt: string;
  if (typeof body.created_at === "string" && Number.isFinite(Date.parse(body.created_at))) occurredAt = new Date(body.created_at).toISOString();
  else if (typeof body.created_at === "number" && Number.isFinite(body.created_at)) occurredAt = new Date(body.created_at * 1000).toISOString();
  else throw new Error("CHATWOOT_WEBHOOK_TIMESTAMP_INVALID");
  return { event: {
    messageId, accountId, inboxId, conversationId, senderId,
    ...(typeof sender.name === "string" && sender.name ? { senderName: sender.name } : {}),
    ...(text ? { text } : {}), attachments, occurredAt
  } };
}

export function createPhase5App(
  db: Database,
  adminToken: string,
  env: Record<string, string | undefined> = process.env,
  dependencies: { matrixGateway?: MatrixGateway } = {}
) {
  const webhookConfigs = new SQLiteChatwootWebhookConfigRepository(db);
  const lookups = new SQLitePhase5LookupRepository(db);
  const chatwootSecrets = new ChatwootEnvironmentSecretProvider(env);
  const webhookSecrets = new ChatwootWebhookEnvironmentSecretProvider(env);
  const service = new ChatwootToMatrixService(
    new SQLiteTenantRepository(db), new SQLiteMetaConnectionRepository(db), new SQLiteChatwootBindingRepository(db),
    new SQLiteConversationBindingRepository(db), new SQLiteProcessedEventRepository(db),
    new ChatwootAttachmentDownloader(chatwootSecrets, env), dependencies.matrixGateway ?? new HttpMatrixGateway(env)
  );

  return new Elysia({ name: "phase5-chatwoot-matrix" })
    .post("/api/v1/chatwoot-bindings/:id/webhook", ({ request, params, body, set }) => {
      if (!tokenMatches(bearer(request), adminToken)) return error(set, 401, "UNAUTHORIZED", "Authentication required");
      const input = body as Record<string, unknown>;
      if (typeof input.secretRef !== "string" || !isSupportedChatwootWebhookSecretRef(input.secretRef)) return error(set, 400, "INVALID_CHATWOOT_WEBHOOK_SECRET_REF", "Only env:CHATWOOT_WEBHOOK_* secret references are supported");
      try {
        const configured = webhookConfigs.set(params.id, input.secretRef);
        return { data: { bindingId: configured.bindingId, secretRef: "[configured]", webhookPath: `/webhooks/chatwoot/${configured.bindingId}` } };
      } catch { return error(set, 404, "CHATWOOT_BINDING_NOT_FOUND", "Chatwoot binding not found"); }
    })
    .post("/webhooks/chatwoot/:bindingId", async ({ request, params, body, set }) => {
      const binding = lookups.findChatwootBinding(params.bindingId);
      const config = webhookConfigs.find(params.bindingId);
      if (!binding || !config || binding.status !== "active") return error(set, 401, "CHATWOOT_WEBHOOK_UNAUTHORIZED", "Webhook authentication failed");
      const secret = webhookSecrets.resolve(config.secretRef);
      if (!secret) return error(set, 503, "CHATWOOT_WEBHOOK_SECRET_MISSING", "Webhook configuration unavailable");
      const rawBody = typeof body === "string" ? body : "";
      const headers = chatwootWebhookHeaders(request);
      if (!verifyChatwootWebhook({ rawBody, secret, timestamp: headers.timestamp, signature: headers.signature })) return error(set, 401, "CHATWOOT_WEBHOOK_UNAUTHORIZED", "Webhook authentication failed");
      let parsed;
      try { parsed = parseChatwootEvent(rawBody); }
      catch (cause) {
        const code = cause instanceof Error ? cause.message : "CHATWOOT_WEBHOOK_INVALID";
        return error(set, 400, code, "Webhook payload is invalid");
      }
      if (parsed.ignored) return { data: { status: "ignored", reason: parsed.ignored } };
      try { return { data: await service.handle(binding, parsed.event!) }; }
      catch (cause) {
        const code = cause instanceof Error ? cause.message : "CHATWOOT_TO_MATRIX_FAILED";
        const terminal = new Set(["CHATWOOT_ROUTE_MISMATCH", "TENANT_NOT_ACTIVE", "CONNECTION_NOT_FOUND", "CONNECTION_NOT_ACTIVE", "CONVERSATION_BINDING_NOT_FOUND", "EMPTY_MESSAGE"]);
        return error(set, terminal.has(code) ? 409 : 503, code, "Chatwoot to Matrix delivery failed");
      }
    }, { parse: "text" });
}
