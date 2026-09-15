import type { Attachment } from "../domain/models";

export type MatrixOutboundAttachment = Attachment & { url: string };

export interface MatrixGateway {
  send(input: {
    roomId: string;
    transactionBase: string;
    sourceEventId: string;
    text?: string;
    attachments: MatrixOutboundAttachment[];
  }): Promise<{ eventIds: string[] }>;
}

export type HttpFetch = (input: string | URL | Request, init?: RequestInit) => Promise<Response>;

function positiveInt(raw: string | undefined, fallback: number): number {
  if (!raw) return fallback;
  const parsed = Number(raw);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback;
}

function validateBase(raw: string): URL {
  let url: URL;
  try { url = new URL(raw); } catch { throw new Error("MATRIX_CLIENT_BASE_URL_INVALID"); }
  if (!new Set(["http:", "https:"]).has(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error("MATRIX_CLIENT_BASE_URL_INVALID");
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

function safeName(attachment: MatrixOutboundAttachment): string {
  const candidate = attachment.fileName?.trim();
  if (candidate && !candidate.includes("/") && !candidate.includes("\\") && !/[\r\n\0]/.test(candidate)) return candidate.slice(0, 255);
  const ext = attachment.mimeType === "image/jpeg" ? ".jpg" : attachment.mimeType === "image/png" ? ".png" : attachment.mimeType === "application/pdf" ? ".pdf" : attachment.mimeType === "audio/ogg" ? ".ogg" : "";
  return `attachment${ext}`;
}

function matrixMessageContent(attachment: MatrixOutboundAttachment, mxc: string, sourceEventId: string): Record<string, unknown> {
  const msgtype = attachment.kind === "image" ? "m.image" : attachment.kind === "audio" ? "m.audio" : attachment.kind === "video" ? "m.video" : "m.file";
  return {
    msgtype,
    body: safeName(attachment),
    url: mxc,
    info: {
      ...(attachment.mimeType ? { mimetype: attachment.mimeType } : {}),
      ...(attachment.sizeBytes != null ? { size: attachment.sizeBytes } : {})
    },
    "com.mautrix_meta_stack.provenance": { source: "chatwoot", source_event_id: sourceEventId }
  };
}

export class HttpMatrixGateway implements MatrixGateway {
  private readonly base: URL;
  private readonly accessToken: string;
  private readonly timeoutMs: number;
  private readonly maxBytes: number;

  constructor(env: Record<string, string | undefined> = process.env, private readonly fetchImpl: HttpFetch = fetch) {
    this.base = validateBase(env.MATRIX_CLIENT_BASE_URL ?? env.MATRIX_MEDIA_BASE_URL ?? "http://synapse:8008");
    this.accessToken = env.MATRIX_CLIENT_ACCESS_TOKEN ?? "";
    this.timeoutMs = positiveInt(env.MATRIX_CLIENT_TIMEOUT_MS, 15_000);
    this.maxBytes = positiveInt(env.MATRIX_CLIENT_MAX_BYTES, 100 * 1024 * 1024);
  }

  private async request(url: URL, init: RequestInit): Promise<Response> {
    if (!this.accessToken) throw new Error("MATRIX_CLIENT_ACCESS_TOKEN_MISSING");
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const headers = new Headers(init.headers);
      headers.set("authorization", `Bearer ${this.accessToken}`);
      return await this.fetchImpl(url, { ...init, headers, signal: controller.signal, redirect: "error" });
    } catch (error) {
      if (controller.signal.aborted) throw new Error("MATRIX_CLIENT_TIMEOUT");
      throw new Error("MATRIX_CLIENT_NETWORK_ERROR", { cause: error });
    } finally { clearTimeout(timer); }
  }

  private async upload(attachment: MatrixOutboundAttachment): Promise<string> {
    const source = new URL(attachment.url);
    if (source.protocol !== "data:") throw new Error("MATRIX_ATTACHMENT_BYTES_REQUIRED");
    const comma = attachment.url.indexOf(",");
    if (comma < 0) throw new Error("MATRIX_ATTACHMENT_BYTES_REQUIRED");
    const meta = attachment.url.slice(5, comma);
    const payload = attachment.url.slice(comma + 1);
    const bytes = meta.endsWith(";base64") ? Uint8Array.from(Buffer.from(payload, "base64")) : new TextEncoder().encode(decodeURIComponent(payload));
    if (bytes.byteLength > this.maxBytes) throw new Error("MATRIX_UPLOAD_TOO_LARGE");
    const url = appendPath(this.base, "/_matrix/media/v3/upload");
    url.searchParams.set("filename", safeName(attachment));
    const response = await this.request(url, { method: "POST", headers: { "content-type": attachment.mimeType ?? "application/octet-stream" }, body: bytes });
    if (response.status !== 200) { await response.body?.cancel().catch(() => undefined); throw new Error(`MATRIX_UPLOAD_HTTP_${response.status}`); }
    const body = await response.json().catch(() => null) as { content_uri?: unknown } | null;
    if (!body || typeof body.content_uri !== "string" || !body.content_uri.startsWith("mxc://")) throw new Error("MATRIX_UPLOAD_RESPONSE_INVALID");
    return body.content_uri;
  }

  private async sendEvent(roomId: string, txnId: string, content: Record<string, unknown>): Promise<string> {
    const url = appendPath(this.base, `/_matrix/client/v3/rooms/${encodeURIComponent(roomId)}/send/m.room.message/${encodeURIComponent(txnId)}`);
    const response = await this.request(url, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(content) });
    if (response.status !== 200) { await response.body?.cancel().catch(() => undefined); throw new Error(`MATRIX_SEND_HTTP_${response.status}`); }
    const body = await response.json().catch(() => null) as { event_id?: unknown } | null;
    if (!body || typeof body.event_id !== "string" || !body.event_id) throw new Error("MATRIX_SEND_RESPONSE_INVALID");
    return body.event_id;
  }

  async send(input: { roomId: string; transactionBase: string; sourceEventId: string; text?: string; attachments: MatrixOutboundAttachment[] }): Promise<{ eventIds: string[] }> {
    const eventIds: string[] = [];
    if (input.text) {
      eventIds.push(await this.sendEvent(input.roomId, `${input.transactionBase}-text`, {
        msgtype: "m.text",
        body: input.text,
        "com.mautrix_meta_stack.provenance": { source: "chatwoot", source_event_id: input.sourceEventId }
      }));
    }
    for (let i = 0; i < input.attachments.length; i++) {
      const attachment = input.attachments[i]!;
      const mxc = await this.upload(attachment);
      eventIds.push(await this.sendEvent(input.roomId, `${input.transactionBase}-a${i}`, matrixMessageContent(attachment, mxc, input.sourceEventId)));
    }
    if (eventIds.length === 0) throw new Error("EMPTY_MESSAGE");
    return { eventIds };
  }
}
