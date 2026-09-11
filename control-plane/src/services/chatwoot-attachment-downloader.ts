import type { ChatwootBinding, SecretProvider } from "../domain/models";
import type { MatrixOutboundAttachment, HttpFetch } from "./matrix-gateway";

export type ChatwootWebhookAttachment = {
  id: string;
  kind: "image" | "video" | "audio" | "file" | "unknown";
  dataUrl: string;
  mimeType?: string;
  fileName?: string;
  sizeBytes?: number;
};

function positiveInt(raw: string | undefined, fallback: number): number {
  if (!raw) return fallback;
  const parsed = Number(raw);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback;
}

async function readBounded(response: Response, maxBytes: number): Promise<Uint8Array> {
  const declared = response.headers.get("content-length");
  if (declared && Number(declared) > maxBytes) throw new Error("CHATWOOT_ATTACHMENT_TOO_LARGE");
  if (!response.body) return new Uint8Array();
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      if (!value) continue;
      total += value.byteLength;
      if (total > maxBytes) throw new Error("CHATWOOT_ATTACHMENT_TOO_LARGE");
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const joined = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) { joined.set(chunk, offset); offset += chunk.byteLength; }
  return joined;
}

export class ChatwootAttachmentDownloader {
  private readonly timeoutMs: number;
  private readonly maxBytes: number;

  constructor(
    private readonly secrets: SecretProvider,
    env: Record<string, string | undefined> = process.env,
    private readonly fetchImpl: HttpFetch = fetch
  ) {
    this.timeoutMs = positiveInt(env.CHATWOOT_ATTACHMENT_TIMEOUT_MS, 15_000);
    this.maxBytes = positiveInt(env.CHATWOOT_ATTACHMENT_MAX_BYTES, 100 * 1024 * 1024);
  }

  private async fetchOnce(url: URL, token: string | null, redirect: RequestRedirect): Promise<Response> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const headers = new Headers();
      if (token) headers.set("api_access_token", token);
      return await this.fetchImpl(url, { headers, signal: controller.signal, redirect });
    } catch (error) {
      if (controller.signal.aborted) throw new Error("CHATWOOT_ATTACHMENT_TIMEOUT");
      throw new Error("CHATWOOT_ATTACHMENT_NETWORK_ERROR", { cause: error });
    } finally { clearTimeout(timer); }
  }

  async download(binding: ChatwootBinding, attachment: ChatwootWebhookAttachment): Promise<MatrixOutboundAttachment> {
    const token = this.secrets.resolve(binding.credentialRef);
    if (!token) throw new Error("CHATWOOT_CREDENTIAL_MISSING");
    const base = new URL(binding.apiBaseUrl);
    let initial: URL;
    try { initial = new URL(attachment.dataUrl, base); } catch { throw new Error("CHATWOOT_ATTACHMENT_URL_INVALID"); }
    if (!new Set(["http:", "https:"]).has(initial.protocol) || initial.username || initial.password || initial.origin !== base.origin) throw new Error("CHATWOOT_ATTACHMENT_ORIGIN_MISMATCH");
    let response = await this.fetchOnce(initial, token, "manual");
    if ([301, 302, 303, 307, 308].includes(response.status)) {
      const location = response.headers.get("location");
      await response.body?.cancel().catch(() => undefined);
      if (!location) throw new Error("CHATWOOT_ATTACHMENT_REDIRECT_INVALID");
      const redirected = new URL(location, initial);
      if (!new Set(["http:", "https:"]).has(redirected.protocol) || redirected.username || redirected.password) throw new Error("CHATWOOT_ATTACHMENT_REDIRECT_INVALID");
      const sameOrigin = redirected.origin === base.origin;
      if (!sameOrigin && redirected.protocol !== "https:") throw new Error("CHATWOOT_ATTACHMENT_REDIRECT_INSECURE");
      response = await this.fetchOnce(redirected, sameOrigin ? token : null, "error");
    }
    if (response.status !== 200) { await response.body?.cancel().catch(() => undefined); throw new Error(`CHATWOOT_ATTACHMENT_HTTP_${response.status}`); }
    const bytes = await readBounded(response, this.maxBytes);
    const mimeType = attachment.mimeType ?? response.headers.get("content-type")?.split(";", 1)[0]?.trim() ?? "application/octet-stream";
    const dataUrl = `data:${mimeType};base64,${Buffer.from(bytes).toString("base64")}`;
    return {
      id: attachment.id,
      kind: attachment.kind,
      url: dataUrl,
      mimeType,
      ...(attachment.fileName ? { fileName: attachment.fileName } : {}),
      sizeBytes: bytes.byteLength
    };
  }
}
