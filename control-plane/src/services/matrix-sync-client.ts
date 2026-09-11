export type MatrixRawEvent = {
  type?: unknown;
  event_id?: unknown;
  state_key?: unknown;
  sender?: unknown;
  origin_server_ts?: unknown;
  content?: unknown;
};

export type MatrixJoinedRoom = {
  state?: { events?: MatrixRawEvent[] };
  timeline?: { events?: MatrixRawEvent[]; limited?: boolean; prev_batch?: string };
};

export type MatrixSyncResponse = {
  next_batch: string;
  rooms: { join: Record<string, MatrixJoinedRoom> };
};

export type MatrixSyncFetch = (input: string | URL | Request, init?: RequestInit) => Promise<Response>;
export type MatrixSyncOptions = { timelineLimit?: number };

function positiveInt(raw: string | undefined, fallback: number): number {
  if (!raw) return fallback;
  const value = Number(raw);
  return Number.isSafeInteger(value) && value > 0 ? value : fallback;
}

function timelineLimit(raw: number | undefined): number {
  const value = raw ?? 100;
  if (!Number.isSafeInteger(value) || value < 0 || value > 1000) throw new Error("MATRIX_SYNC_TIMELINE_LIMIT_INVALID");
  return value;
}

function validateBase(raw: string): URL {
  let url: URL;
  try { url = new URL(raw); } catch { throw new Error("MATRIX_SYNC_BASE_URL_INVALID"); }
  if (!new Set(["http:", "https:"]).has(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error("MATRIX_SYNC_BASE_URL_INVALID");
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

async function readBoundedJson(response: Response, maxBytes: number): Promise<unknown> {
  const declared = response.headers.get("content-length");
  if (declared) {
    const parsed = Number(declared);
    if (Number.isFinite(parsed) && parsed > maxBytes) throw new Error("MATRIX_SYNC_RESPONSE_TOO_LARGE");
  }
  if (!response.body) throw new Error("MATRIX_SYNC_RESPONSE_INVALID");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      if (!value) continue;
      total += value.byteLength;
      if (total > maxBytes) throw new Error("MATRIX_SYNC_RESPONSE_TOO_LARGE");
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  try { return JSON.parse(new TextDecoder().decode(bytes)); }
  catch { throw new Error("MATRIX_SYNC_RESPONSE_INVALID"); }
}

function parseEvents(value: unknown): MatrixRawEvent[] {
  if (!Array.isArray(value)) return [];
  return value.filter((entry): entry is MatrixRawEvent => !!entry && typeof entry === "object");
}

function parseSyncResponse(value: unknown): MatrixSyncResponse {
  if (!value || typeof value !== "object") throw new Error("MATRIX_SYNC_RESPONSE_INVALID");
  const root = value as Record<string, unknown>;
  if (typeof root.next_batch !== "string" || !root.next_batch) throw new Error("MATRIX_SYNC_RESPONSE_INVALID");
  const rawRooms = root.rooms && typeof root.rooms === "object" ? root.rooms as Record<string, unknown> : {};
  const rawJoin = rawRooms.join && typeof rawRooms.join === "object" ? rawRooms.join as Record<string, unknown> : {};
  const join: Record<string, MatrixJoinedRoom> = {};
  for (const [roomId, rawRoom] of Object.entries(rawJoin)) {
    if (!roomId.startsWith("!") || !rawRoom || typeof rawRoom !== "object") continue;
    const room = rawRoom as Record<string, unknown>;
    const state = room.state && typeof room.state === "object" ? room.state as Record<string, unknown> : {};
    const timeline = room.timeline && typeof room.timeline === "object" ? room.timeline as Record<string, unknown> : {};
    join[roomId] = {
      state: { events: parseEvents(state.events) },
      timeline: {
        events: parseEvents(timeline.events),
        ...(typeof timeline.limited === "boolean" ? { limited: timeline.limited } : {}),
        ...(typeof timeline.prev_batch === "string" ? { prev_batch: timeline.prev_batch } : {})
      }
    };
  }
  return { next_batch: root.next_batch, rooms: { join } };
}

export class HttpMatrixSyncClient {
  private readonly base: URL;
  private readonly token: string;
  private readonly serverTimeoutMs: number;
  private readonly requestTimeoutMs: number;
  private readonly maxResponseBytes: number;

  constructor(env: Record<string, string | undefined> = process.env, private readonly fetchImpl: MatrixSyncFetch = fetch) {
    this.base = validateBase(env.MATRIX_SYNC_BASE_URL ?? env.MATRIX_CLIENT_BASE_URL ?? "http://synapse:8008");
    this.token = env.MATRIX_SYNC_ACCESS_TOKEN ?? "";
    this.serverTimeoutMs = positiveInt(env.MATRIX_SYNC_SERVER_TIMEOUT_MS, 20_000);
    this.requestTimeoutMs = positiveInt(env.MATRIX_SYNC_REQUEST_TIMEOUT_MS, this.serverTimeoutMs + 5_000);
    this.maxResponseBytes = positiveInt(env.MATRIX_SYNC_MAX_RESPONSE_BYTES, 8 * 1024 * 1024);
    if (this.requestTimeoutMs <= this.serverTimeoutMs) throw new Error("MATRIX_SYNC_TIMEOUT_CONFIGURATION_INVALID");
  }

  private async request(url: URL): Promise<Response> {
    if (!this.token) throw new Error("MATRIX_SYNC_ACCESS_TOKEN_MISSING");
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.requestTimeoutMs);
    try {
      return await this.fetchImpl(url, {
        method: "GET",
        headers: { authorization: `Bearer ${this.token}`, accept: "application/json" },
        signal: controller.signal,
        redirect: "error"
      });
    } catch (error) {
      if (controller.signal.aborted) throw new Error("MATRIX_SYNC_TIMEOUT");
      throw new Error("MATRIX_SYNC_NETWORK_ERROR", { cause: error });
    } finally { clearTimeout(timer); }
  }

  async sync(since: string | null, options: MatrixSyncOptions = {}): Promise<MatrixSyncResponse> {
    const limit = timelineLimit(options.timelineLimit);
    const url = appendPath(this.base, "/_matrix/client/v3/sync");
    url.searchParams.set("timeout", String(this.serverTimeoutMs));
    url.searchParams.set("filter", JSON.stringify({
      room: {
        state: { types: ["m.bridge", "uk.half-shot.bridge"] },
        timeline: { types: ["m.room.message"], limit },
        ephemeral: { types: [] },
        account_data: { types: [] }
      },
      presence: { types: [] },
      account_data: { types: [] }
    }));
    if (since) url.searchParams.set("since", since);
    const response = await this.request(url);
    if (response.status !== 200) {
      await response.body?.cancel().catch(() => undefined);
      throw new Error(response.status === 401 || response.status === 403 ? "MATRIX_SYNC_UNAUTHORIZED" : `MATRIX_SYNC_HTTP_${response.status}`);
    }
    return parseSyncResponse(await readBoundedJson(response, this.maxResponseBytes));
  }

  async roomState(roomId: string): Promise<MatrixRawEvent[]> {
    if (!roomId.startsWith("!")) throw new Error("MATRIX_ROOM_ID_INVALID");
    const url = appendPath(this.base, `/_matrix/client/v3/rooms/${encodeURIComponent(roomId)}/state`);
    const response = await this.request(url);
    if (response.status !== 200) {
      await response.body?.cancel().catch(() => undefined);
      if (response.status === 401 || response.status === 403) throw new Error("MATRIX_SYNC_UNAUTHORIZED");
      if (response.status === 404) throw new Error("MATRIX_ROOM_NOT_FOUND");
      throw new Error(`MATRIX_ROOM_STATE_HTTP_${response.status}`);
    }
    const value = await readBoundedJson(response, this.maxResponseBytes);
    if (!Array.isArray(value)) throw new Error("MATRIX_ROOM_STATE_INVALID");
    return parseEvents(value);
  }
}
