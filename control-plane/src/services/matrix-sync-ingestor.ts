import type {
  Attachment,
  MatrixEncryptedFile,
  MatrixRoomBinding,
  MatrixRoomBindingRepository,
  MatrixSyncCheckpointRepository
} from "../domain/models";
import type { MatrixToChatwootService } from "./matrix-to-chatwoot";
import { MatrixRoomAttributionService } from "./matrix-room-attribution";
import type { HttpMatrixSyncClient, MatrixJoinedRoom, MatrixRawEvent, MatrixSyncResponse } from "./matrix-sync-client";

const REMOTE_SENDER_ID_KEY = "com.mautrix_meta_stack.remote_sender_id";
const PROVENANCE_KEY = "com.mautrix_meta_stack.provenance";
const MATRIX_VOICE_KEY = "org.matrix.msc3245.voice";

type MatrixSyncTransport = Pick<HttpMatrixSyncClient, "sync" | "roomState" | "joinRoom"> & Partial<Pick<HttpMatrixSyncClient, "roomMessages">>;

export type MatrixSyncRunResult = {
  status: "bootstrapped" | "processed";
  fromCheckpoint: string | null;
  nextCheckpoint: string;
  roomsSeen: number;
  roomsJoined: number;
  roomsBound: number;
  eventsDelivered: number;
  eventsIgnored: number;
};

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.trim() === value && value.length > 0 ? value : null;
}

function safePositiveNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : undefined;
}

function configuredPositiveInt(raw: string | undefined, fallback: number, max: number): number {
  if (!raw) return fallback;
  const parsed = Number(raw);
  if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > max) throw new Error("MATRIX_SYNC_GAP_LIMIT_INVALID");
  return parsed;
}

function parseEncryptedFile(raw: Record<string, unknown>): { url: string; encryption: MatrixEncryptedFile } | null {
  const url = nonEmptyString(raw.url);
  const key = record(raw.key);
  const hashes = record(raw.hashes);
  const keyOps = key && Array.isArray(key.key_ops) ? key.key_ops.filter((entry): entry is string => typeof entry === "string") : [];
  if (
    !url || !url.startsWith("mxc://") || raw.v !== "v2" || !key || !hashes ||
    key.kty !== "oct" || key.alg !== "A256CTR" || key.ext !== true ||
    typeof key.k !== "string" || typeof raw.iv !== "string" || typeof hashes.sha256 !== "string" ||
    !keyOps.includes("decrypt")
  ) return null;
  return {
    url,
    encryption: {
      v: "v2",
      key: { kty: "oct", alg: "A256CTR", k: key.k, keyOps, ext: true },
      iv: raw.iv,
      hashes: { sha256: hashes.sha256 }
    }
  };
}

function parseAttachment(content: Record<string, unknown>, eventId: string): Attachment | null {
  const msgtype = nonEmptyString(content.msgtype);
  const kind = msgtype === "m.image" ? "image"
    : msgtype === "m.video" ? "video"
    : msgtype === "m.audio" ? "audio"
    : msgtype === "m.file" ? "file"
    : null;
  if (!kind) return null;

  const info = record(content.info);
  const directUrl = nonEmptyString(content.url);
  const encryptedRaw = record(content.file);
  const encrypted = encryptedRaw ? parseEncryptedFile(encryptedRaw) : null;
  if (encryptedRaw && !encrypted) throw new Error("MATRIX_MEDIA_ENCRYPTION_INVALID");
  const url = encrypted?.url ?? directUrl;
  if (!url || !url.startsWith("mxc://")) return null;

  const mimeType = info ? nonEmptyString(info.mimetype) : null;
  const fileName = nonEmptyString(content.filename) ?? nonEmptyString(content.body);
  const sizeBytes = info ? safePositiveNumber(info.size) : undefined;
  const voiceNote = kind === "audio" && record(content[MATRIX_VOICE_KEY]) !== null;
  return {
    id: eventId,
    kind,
    url,
    ...(mimeType ? { mimeType } : {}),
    ...(fileName ? { fileName } : {}),
    ...(sizeBytes != null ? { sizeBytes } : {}),
    ...(voiceNote ? { voiceNote: true } : {}),
    ...(encrypted ? { encryption: encrypted.encryption } : {})
  };
}

function occurredAt(raw: unknown): string {
  if (typeof raw !== "number" || !Number.isSafeInteger(raw) || raw < 0) throw new Error("MATRIX_EVENT_TIMESTAMP_INVALID");
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) throw new Error("MATRIX_EVENT_TIMESTAMP_INVALID");
  return date.toISOString();
}

function provenanceSource(content: Record<string, unknown>): string | null {
  const provenance = record(content[PROVENANCE_KEY]);
  return nonEmptyString(provenance?.source);
}

function bridgeStatePresent(events: MatrixRawEvent[]): boolean {
  return events.some((event) => event.type === "m.bridge" || event.type === "uk.half-shot.bridge");
}

function deduplicateEvents(events: MatrixRawEvent[]): MatrixRawEvent[] {
  const seen = new Set<string>();
  const result: MatrixRawEvent[] = [];
  for (const event of events) {
    const eventId = nonEmptyString(event.event_id);
    if (eventId) {
      if (seen.has(eventId)) continue;
      seen.add(eventId);
    }
    result.push(event);
  }
  return result;
}

export class MatrixSyncIngestor {
  private readonly syncUserMxid: string;
  private readonly gapPageLimit: number;
  private readonly maxGapPages: number;
  private readonly maxGapEvents: number;

  constructor(
    private readonly consumerId: string,
    private readonly sync: MatrixSyncTransport,
    private readonly attribution: MatrixRoomAttributionService,
    private readonly roomBindings: MatrixRoomBindingRepository,
    private readonly checkpoints: MatrixSyncCheckpointRepository,
    private readonly matrixToChatwoot: MatrixToChatwootService,
    env: Record<string, string | undefined> = process.env
  ) {
    if (!consumerId.trim() || consumerId.trim() !== consumerId) throw new Error("MATRIX_SYNC_CONSUMER_ID_INVALID");
    this.syncUserMxid = env.MATRIX_SYNC_USER_MXID ?? "";
    if (!this.syncUserMxid.startsWith("@") || !this.syncUserMxid.includes(":")) throw new Error("MATRIX_SYNC_USER_MXID_INVALID");
    this.gapPageLimit = configuredPositiveInt(env.MATRIX_SYNC_GAP_PAGE_LIMIT, 100, 1000);
    this.maxGapPages = configuredPositiveInt(env.MATRIX_SYNC_MAX_GAP_PAGES, 20, 1000);
    this.maxGapEvents = configuredPositiveInt(env.MATRIX_SYNC_MAX_GAP_EVENTS, 2000, 100_000);
  }

  private async acceptTrustedInvites(response: MatrixSyncResponse): Promise<number> {
    let roomsJoined = 0;
    for (const [roomId, room] of Object.entries(response.rooms.invite)) {
      const events = room.invite_state?.events ?? [];
      if (!this.attribution.isTrustedInvite(events, this.syncUserMxid)) continue;
      await this.sync.joinRoom(roomId);
      roomsJoined++;
    }
    return roomsJoined;
  }

  private async bindFromCurrentState(roomId: string, stateEvents: MatrixRawEvent[]): Promise<MatrixRoomBinding | null> {
    let events = stateEvents;
    if (!bridgeStatePresent(events)) events = await this.sync.roomState(roomId);
    try { return this.attribution.bindRoom(roomId, events); }
    catch (error) {
      if (error instanceof Error && ["MATRIX_BRIDGE_STATE_NOT_FOUND", "MATRIX_BRIDGE_CONNECTION_NOT_ACTIVE"].includes(error.message)) return null;
      throw error;
    }
  }

  private async verifyRoom(roomId: string, room: MatrixJoinedRoom, requireCurrentState: boolean): Promise<MatrixRoomBinding | null> {
    const stateEvents = room.state?.events ?? [];
    if (requireCurrentState) return this.bindFromCurrentState(roomId, stateEvents);

    const existing = this.roomBindings.findByRoomId(roomId);
    if (bridgeStatePresent(stateEvents)) {
      try { return this.attribution.bindRoom(roomId, stateEvents); }
      catch (error) {
        if (error instanceof Error && error.message === "MATRIX_BRIDGE_CONNECTION_NOT_ACTIVE") return null;
        if (error instanceof Error && error.message === "MATRIX_BRIDGE_STATE_NOT_FOUND" && existing) return existing;
        throw error;
      }
    }
    if (existing) return existing;
    return this.bindFromCurrentState(roomId, stateEvents);
  }

  private async reconcileRooms(response: MatrixSyncResponse, requireCurrentState: boolean): Promise<number> {
    let roomsBound = 0;
    for (const [roomId, room] of Object.entries(response.rooms.join)) {
      const binding = await this.verifyRoom(roomId, room, requireCurrentState);
      if (binding) roomsBound++;
    }
    return roomsBound;
  }

  private async recoverGap(roomId: string, from: string, to: string | undefined): Promise<MatrixRawEvent[]> {
    if (!to) throw new Error("MATRIX_SYNC_GAP_PREV_BATCH_REQUIRED");
    if (!this.sync.roomMessages) throw new Error("MATRIX_SYNC_GAP_RECOVERY_UNAVAILABLE");
    if (from === to) return [];

    let cursor = from;
    const recovered: MatrixRawEvent[] = [];
    const seen = new Set<string>();
    for (let pageIndex = 0; pageIndex < this.maxGapPages; pageIndex++) {
      const page = await this.sync.roomMessages(roomId, cursor, to, this.gapPageLimit);
      for (const event of page.chunk) {
        const eventId = nonEmptyString(event.event_id);
        if (eventId) {
          if (seen.has(eventId)) continue;
          seen.add(eventId);
        }
        recovered.push(event);
        if (recovered.length > this.maxGapEvents) throw new Error("MATRIX_SYNC_GAP_TOO_LARGE");
      }
      if (page.end === null || page.end === to) return recovered;
      if (page.end === cursor) throw new Error("MATRIX_SYNC_GAP_NOT_CONVERGED");
      cursor = page.end;
    }
    throw new Error("MATRIX_SYNC_GAP_TOO_LARGE");
  }

  private async eventsForRoom(roomId: string, room: MatrixJoinedRoom, checkpoint: string): Promise<MatrixRawEvent[]> {
    const timeline = room.timeline;
    const current = timeline?.events ?? [];
    if (!timeline?.limited) return current;
    const recovered = await this.recoverGap(roomId, checkpoint, timeline.prev_batch);
    return deduplicateEvents([...recovered, ...current]);
  }

  private async processEvent(binding: MatrixRoomBinding, roomId: string, event: MatrixRawEvent): Promise<"delivered" | "ignored"> {
    if (event.type !== "m.room.message") return "ignored";
    const eventId = nonEmptyString(event.event_id);
    const sender = nonEmptyString(event.sender);
    const content = record(event.content);
    if (!eventId || !sender || !content) return "ignored";

    const provenance = provenanceSource(content);
    if (provenance !== "meta") return "ignored";

    const remoteContactId = nonEmptyString(content[REMOTE_SENDER_ID_KEY]);
    if (!remoteContactId) throw new Error("MATRIX_REMOTE_SENDER_ID_REQUIRED");
    const msgtype = nonEmptyString(content.msgtype);
    const text = msgtype === "m.text" ? nonEmptyString(content.body) : null;
    const attachment = parseAttachment(content, eventId);
    if (!text && !attachment) return "ignored";

    await this.matrixToChatwoot.handle({
      connectionId: binding.metaConnectionId,
      roomId,
      remoteThreadId: binding.remoteThreadId,
      remoteContactId,
      eventId,
      senderId: sender,
      ...(text ? { text } : {}),
      ...(attachment ? { attachments: [attachment] } : {}),
      occurredAt: occurredAt(event.origin_server_ts),
      provenance: "meta"
    });
    return "delivered";
  }

  async runOnce(): Promise<MatrixSyncRunResult> {
    const checkpoint = this.checkpoints.get(this.consumerId);
    if (!checkpoint) {
      const response = await this.sync.sync(null, { timelineLimit: 0 });
      const roomsJoined = await this.acceptTrustedInvites(response);
      const roomsBound = await this.reconcileRooms(response, true);
      this.checkpoints.save(this.consumerId, response.next_batch);
      return {
        status: "bootstrapped",
        fromCheckpoint: null,
        nextCheckpoint: response.next_batch,
        roomsSeen: Object.keys(response.rooms.join).length + Object.keys(response.rooms.invite).length,
        roomsJoined,
        roomsBound,
        eventsDelivered: 0,
        eventsIgnored: 0
      };
    }

    const response = await this.sync.sync(checkpoint.nextBatch, { timelineLimit: 100 });
    const roomsJoined = await this.acceptTrustedInvites(response);

    let roomsBound = 0;
    let eventsDelivered = 0;
    let eventsIgnored = 0;
    for (const [roomId, room] of Object.entries(response.rooms.join)) {
      const binding = await this.verifyRoom(roomId, room, false);
      if (!binding) {
        eventsIgnored += room.timeline?.events?.length ?? 0;
        continue;
      }
      roomsBound++;
      const events = await this.eventsForRoom(roomId, room, checkpoint.nextBatch);
      for (const event of events) {
        const result = await this.processEvent(binding, roomId, event);
        if (result === "delivered") eventsDelivered++;
        else eventsIgnored++;
      }
    }

    this.checkpoints.save(this.consumerId, response.next_batch);
    return {
      status: "processed",
      fromCheckpoint: checkpoint.nextBatch,
      nextCheckpoint: response.next_batch,
      roomsSeen: Object.keys(response.rooms.join).length + Object.keys(response.rooms.invite).length,
      roomsJoined,
      roomsBound,
      eventsDelivered,
      eventsIgnored
    };
  }
}
