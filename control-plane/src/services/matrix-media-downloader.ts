import type { Attachment, MatrixEncryptedFile } from "../domain/models";

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

function decodeBase64(raw: string, urlSafe = false): Uint8Array {
  if (!raw || /\s/.test(raw)) throw new Error("MATRIX_MEDIA_ENCRYPTION_INVALID");
  let value = urlSafe ? raw.replace(/-/g, "+").replace(/_/g, "/") : raw;
  const remainder = value.length % 4;
  if (remainder === 1) throw new Error("MATRIX_MEDIA_ENCRYPTION_INVALID");
  if (remainder > 0) value += "=".repeat(4 - remainder);
  try { return new Uint8Array(Buffer.from(value, "base64")); } catch { throw new Error("MATRIX_MEDIA_ENCRYPTION_INVALID"); }
}

function toArrayBuffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
}

function constantTimeEqual(left: Uint8Array, right: Uint8Array): boolean {
  if (left.byteLength !== right.byteLength) return false;
  let diff = 0;
  for (let i = 0; i < left.byteLength; i++) diff |= left[i]! ^ right[i]!;
  return diff === 0;
}

async function decryptEncryptedFile(ciphertext: Uint8Array, encrypted: MatrixEncryptedFile): Promise<Uint8Array> {
  if (encrypted.v !== "v2" || encrypted.key.kty !== "oct" || encrypted.key.alg !== "A256CTR" || encrypted.key.ext !== true || !encrypted.key.keyOps.includes("decrypt")) {
    throw new Error("MATRIX_MEDIA_ENCRYPTION_INVALID");
  }
  const keyBytes = decodeBase64(encrypted.key.k, true);
  const iv = decodeBase64(encrypted.iv);
  const expectedHash = decodeBase64(encrypted.hashes.sha256);
  if (keyBytes.byteLength !== 32 || iv.byteLength !== 16 || expectedHash.byteLength !== 32) throw new Error("MATRIX_MEDIA_ENCRYPTION_INVALID");

  const actualHash = new Uint8Array(await crypto.subtle.digest("SHA-256", toArrayBuffer(ciphertext)));
  if (!constantTimeEqual(actualHash, expectedHash)) throw new Error("MATRIX_MEDIA_HASH_MISMATCH");

  try {
    const key = await crypto.subtle.importKey("raw", toArrayBuffer(keyBytes), { name: "AES-CTR" }, false, ["decrypt"]);
    const plaintext = await crypto.subtle.decrypt({ name: "AES-CTR", counter: toArrayBuffer(iv), length: 64 }, key, toArrayBuffer(ciphertext));
    return new Uint8Array(plaintext);
  } catch (error) {
    throw new Error("MATRIX_MEDIA_DECRYPT_FAILED", { cause: error });
  }
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
    if (attachment.sizeBytes != null && attachment.sizeBytes > this.maxBytes) throw new Error("MATRIX_MEDIA_TOO_LARGE");
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

    const ciphertext = await readBounded(response, this.maxBytes);
    const bytes = attachment.encryption ? await decryptEncryptedFile(ciphertext, attachment.encryption) : ciphertext;
    if (bytes.byteLength > this.maxBytes) throw new Error("MATRIX_MEDIA_TOO_LARGE");
    const responseType = response.headers.get("content-type")?.split(";", 1)[0]?.trim();
    const mimeType = attachment.mimeType?.trim() || responseType || "application/octet-stream";
    return {
      blob: new Blob([toArrayBuffer(bytes)], { type: mimeType }),
      fileName: safeFileName(attachment),
      mimeType,
      sizeBytes: bytes.byteLength
    };
  }
}
