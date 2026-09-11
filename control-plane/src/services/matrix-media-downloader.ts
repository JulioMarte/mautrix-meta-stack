import type { Attachment } from "../domain/models";

export type DownloadedAttachment = {
  blob: Blob;
  fileName: string;
  mimeType: string;
  sizeBytes: number;
};

export interface MatrixMediaDownloader {
  download(attachment: Attachment): Promise<DownloadedAttachment>;
}

export type HttpFetch = (input: string | URL | Request, init?: RequestInit) => Promise<Response>;

function parsePositiveInt(raw: string | undefined, fallback: number): number {
  if (!raw) return fallback;
  const value = Number(raw);
  return Number.isSafeInteger(value) && value > 0 ? value : fallback;
}

function validatedHomeserverBase(raw: string): URL {
  let url: URL;
  try { url = new URL(raw); } catch { throw new Error("MATRIX_MEDIA_BASE_URL_INVALID"); }
  if (!new Set(["http:", "https:"]).has(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error("MATRIX_MEDIA_BASE_URL_INVALID");
  }
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

function parseMxc(raw: string): { serverName: string; mediaId: string } {
  let url: URL;
  try { url = new URL(raw); } catch { throw new Error("MATRIX_MEDIA_URI_INVALID"); }
  if (url.protocol !== "mxc:" || !url.host || url.username || url.password || url.search || url.hash) throw new Error("MATRIX_MEDIA_URI_INVALID");
  const mediaId = url.pathname.replace(/^\/+/, "");
  if (!mediaId || mediaId.includes("/")) throw new Error("MATRIX_MEDIA_URI_INVALID");
  return { serverName: url.host, mediaId };
}

function safeFileName(attachment: Attachment): string {
  const candidate = attachment.fileName?.trim();
  if (candidate && !candidate.includes("/") && !candidate.includes("\\") && !/[\r\n\0]/.test(candidate)) return candidate.slice(0, 255);
  const extension = attachment.mimeType === "image/jpeg" ? ".jpg"
    : attachment.mimeType === "image/png" ? ".png"
    : attachment.mimeType === "audio/ogg" ? ".ogg"
    : attachment.mimeType === "audio/mpeg" ? ".mp3"
    : attachment.mimeType === "application/pdf" ? ".pdf"
    : "";
  return `attachment${extension}`;
}

async function readBounded(response: Response, maxBytes: number): Promise<Uint8Array> {
  const declared = response.headers.get("content-length");
  if (declared) {
    const parsed = Number(declared);
    if (Number.isFinite(parsed) && parsed > maxBytes) throw new Error("MATRIX_MEDIA_TOO_LARGE");
  }
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
      if (total > maxBytes) throw new Error("MATRIX_MEDIA_TOO_LARGE");
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const joined = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) { joined.set(chunk, offset); offset += chunk.byteLength; }
  return joined;
}

export class HttpMatrixMediaDownloader implements MatrixMediaDownloader {
  private readonly base: URL;
  private readonly accessToken: string;
  private readonly timeoutMs: number;
  private readonly maxBytes: number;

  constructor(
    env: Record<string, string | undefined> = process.env,
    private readonly fetchImpl: HttpFetch = fetch
  ) {
    this.base = validatedHomeserverBase(env.MATRIX_MEDIA_BASE_URL ?? "http://synapse:8008");
    this.accessToken = env.MATRIX_MEDIA_ACCESS_TOKEN ?? "";
    this.timeoutMs = parsePositiveInt(env.MATRIX_MEDIA_TIMEOUT_MS, 15_000);
    this.maxBytes = parsePositiveInt(env.MATRIX_MEDIA_MAX_BYTES, 100 * 1024 * 1024);
  }

  private async fetchWithTimeout(url: URL, includeAuth: boolean, redirect: RequestRedirect): Promise<Response> {
    if (includeAuth && !this.accessToken) throw new Error("MATRIX_MEDIA_ACCESS_TOKEN_MISSING");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const headers = new Headers();
      if (includeAuth) headers.set("authorization", `Bearer ${this.accessToken}`);
      return await this.fetchImpl(url, { headers, signal: controller.signal, redirect });
    } catch (error) {
      if (controller.signal.aborted) throw new Error("MATRIX_MEDIA_TIMEOUT");
      throw new Error("MATRIX_MEDIA_NETWORK_ERROR", { cause: error });
    } finally {
      clearTimeout(timeout);
    }
  }

  async download(attachment: Attachment): Promise<DownloadedAttachment> {
    if (!attachment.url) throw new Error("MATRIX_MEDIA_URI_REQUIRED");
    const { serverName, mediaId } = parseMxc(attachment.url);
    const initialUrl = appendPath(this.base, `/_matrix/client/v1/media/download/${encodeURIComponent(serverName)}/${encodeURIComponent(mediaId)}`);
    let response = await this.fetchWithTimeout(initialUrl, true, "manual");

    if (response.status === 307 || response.status === 308) {
      const location = response.headers.get("location");
      await response.body?.cancel().catch(() => undefined);
      if (!location) throw new Error("MATRIX_MEDIA_REDIRECT_INVALID");
      const redirected = new URL(location, initialUrl);
      if (!new Set(["http:", "https:"]).has(redirected.protocol) || redirected.username || redirected.password) throw new Error("MATRIX_MEDIA_REDIRECT_INVALID");
      const sameOrigin = redirected.origin === this.base.origin;
      if (!sameOrigin && redirected.protocol !== "https:") throw new Error("MATRIX_MEDIA_REDIRECT_INSECURE");
      response = await this.fetchWithTimeout(redirected, sameOrigin, "error");
    }

    if (response.status !== 200) {
      await response.body?.cancel().catch(() => undefined);
      throw new Error(`MATRIX_MEDIA_HTTP_${response.status}`);
    }

    const bytes = await readBounded(response, this.maxBytes);
    if (attachment.sizeBytes != null && attachment.sizeBytes > this.maxBytes) throw new Error("MATRIX_MEDIA_TOO_LARGE");
    const responseType = response.headers.get("content-type")?.split(";", 1)[0]?.trim();
    const mimeType = attachment.mimeType?.trim() || responseType || "application/octet-stream";
    const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
    return {
      blob: new Blob([buffer], { type: mimeType }),
      fileName: safeFileName(attachment),
      mimeType,
      sizeBytes: bytes.byteLength
    };
  }
}
