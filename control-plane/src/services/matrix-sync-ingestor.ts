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

type MatrixSyncTransport = Pick<HttpMatrixSyncClient, "sync" | "roomState">;

export type MatrixSyncRunResult = {
  status: "bootstrapped" | "processed";
  fromCheckpoint: string | null;
  nextCheckpoint: string;
  roomsSeen: number;
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

function parseEncryptedFile(raw: Record<string, unknown>): { url: string; encryption: MatrixEncryptedFile } | null {
  const url = nonEmptyString(raw.url);
  const key = record(raw.key);
  const hashes = record(raw.hashes);
  const keyOps = key && Array.isArray(key.key_ops) ? key.key_ops.filter((entry): entry is string => typeof entry === "string") : [];
  if (
    !url || !url.startsWith("mxc://") || raw.v !== "v2" || !key || !hashes ||
    key.kty !== "oct" || key.alg !== "A256CTR" || key.ext !== true ||
    typeof key.k !== "string" || typeof raw.iv !== "string" || typeof hashes.sha256 !== "string"
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
  const url = encrypted?.url ?? directUrl;
  if (!url || !url.startsWith("mxc://")) return null;

  const mimeType = info ? nonEmptyString(info.mimetype) : null;
  const fileName = nonEmptyString(content.filename) ?? nonEmptyString(content.body);
  const sizeBytes = info ? safePositiveNumber(info.size) : undefined;
  return {
    id: eventId,
    kind,
    url,
    ...(mimeType ? { mimeType } : {}),
    ...(fileName ? { fileName } : {}),
    ...(sizeBytes != null ? { sizeBytes } : {}),
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

export class MatrixSyncIngestor {
  constructor(
    private readonly consumerId: string,
    private readonly sync: MatrixSyncTransport,
    private readonly attribution: MatrixRoomAttributionService,
    private readonly roomBindings: MatrixRoomBindingRepository,
    private readonly checkpoints: MatrixSyncCheckpointRepository,
    private readonly matrixToChatwoot: MatrixToChatwootService
  ) {
    if (!consumerId.trim() || consumerId.trim() !== consumerId) throw new Error("MATRIX_SYNC_CONSUMER_ID_INVALID");
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
      const roomsBound = await this.reconcileRooms(response, true);
      this.checkpoints.save(this.consumerId, response.next_batch);
      return {
        status: "bootstrapped",
        fromCheckpoint: null,
        nextCheckpoint: response.next_batch,
        roomsSeen: Object.keys(response.rooms.join).length,
        roomsBound,
        eventsDelivered: 0,
        eventsIgnored: 0
      };
    }

    const response = await this.sync.sync(checkpoint.nextBatch, { timelineLimit: 100 });
    for (const room of Object.values(response.rooms.join)) {
      if (room.timeline?.limited) throw new Error("MATRIX_SYNC_TIMELINE_GAP");
    }

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
      for (const event of room.timeline?.events ?? []) {
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
      roomsSeen: Object.keys(response.rooms.join).length,
      roomsBound,
      eventsDelivered,
      eventsIgnored
    };
  }
}
