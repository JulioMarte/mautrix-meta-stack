import { afterAll, describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Database } from "bun:sqlite";
import type { MatrixToChatwootService } from "../src/services/matrix-to-chatwoot";
import { runMigrations } from "../src/persistence/migrations";
import {
  SQLiteMatrixRoomBindingRepository,
  SQLiteMatrixSyncCheckpointRepository
} from "../src/persistence/sqlite-matrix-repositories";
import { SQLiteMetaConnectionRepository, SQLiteTenantRepository } from "../src/persistence/sqlite-repositories";
import { MatrixRoomAttributionService } from "../src/services/matrix-room-attribution";
import { HttpMatrixSyncClient } from "../src/services/matrix-sync-client";
import { MatrixSyncIngestor } from "../src/services/matrix-sync-ingestor";

const base = process.env.MATRIX_LIVE_BASE_URL ?? "";
const botToken = process.env.MATRIX_LIVE_BOT_ACCESS_TOKEN ?? "";
const syncToken = process.env.MATRIX_LIVE_SYNC_ACCESS_TOKEN ?? "";
const botMxid = process.env.MATRIX_LIVE_BOT_MXID ?? "";
const syncMxid = process.env.MATRIX_LIVE_SYNC_MXID ?? "";
const enabled = base && botToken && syncToken && botMxid && syncMxid;
const tempDir = enabled ? mkdtempSync(join(tmpdir(), "matrix-live-")) : "";

afterAll(() => { if (tempDir) rmSync(tempDir, { recursive: true, force: true }); });

async function matrixRequest(method: string, path: string, token: string, body?: unknown) {
  const response = await fetch(`${base}${path}`, {
    method,
    headers: {
      authorization: `Bearer ${token}`,
      ...(body === undefined ? {} : { "content-type": "application/json" })
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) })
  });
  const text = await response.text();
  let parsed: any = null;
  try { parsed = text ? JSON.parse(text) : null; } catch {}
  if (!response.ok) throw new Error(`Matrix ${method} ${path} failed ${response.status}: ${text}`);
  return parsed;
}

function fakeService(deliveries: any[]): MatrixToChatwootService {
  return {
    async handle(event: any) {
      deliveries.push(event);
      return { status: "delivered", processedEventId: `p-${deliveries.length}`, conversationBindingId: "c", chatwootMessageId: `m-${deliveries.length}` };
    }
  } as unknown as MatrixToChatwootService;
}

describe.skipIf(!enabled)("live Synapse Matrix ingestion", () => {
  test("trusted invite auto-joins, m.bridge attributes room and real timeline events are ingested", async () => {
    const created = await matrixRequest("POST", "/_matrix/client/v3/createRoom", botToken, {
      preset: "private_chat",
      invite: [syncMxid],
      initial_state: [{
        type: "m.bridge",
        state_key: "facebookgo://thread-live",
        content: {
          protocol: { id: "facebookgo" },
          channel: { id: "thread-live", receiver: "login-live" }
        }
      }]
    });
    const roomId = created?.room_id;
    expect(typeof roomId).toBe("string");

    const dbPath = join(tempDir, "control-plane.db");
    const database = new Database(dbPath, { create: true, strict: true });
    runMigrations(database);
    const tenants = new SQLiteTenantRepository(database);
    const connections = new SQLiteMetaConnectionRepository(database);
    const tenant = tenants.create({ slug: "live", name: "Live" });
    const connection = connections.create({ tenantId: tenant.id, matrixOwnerMxid: "@owner:matrix.example.com" });
    connections.setProviderIdentity(connection.id, { metaAccountId: "1001", mautrixLoginId: "login-live" });
    connections.setStatus(connection.id, "active");
    const rooms = new SQLiteMatrixRoomBindingRepository(database);
    const checkpoints = new SQLiteMatrixSyncCheckpointRepository(database);
    const attribution = new MatrixRoomAttributionService(connections, rooms, {
      MATRIX_BRIDGE_BOT_MXID: botMxid,
      MATRIX_BRIDGE_PROTOCOL_IDS: "facebookgo"
    });
    const client = new HttpMatrixSyncClient({
      MATRIX_SYNC_BASE_URL: base,
      MATRIX_SYNC_ACCESS_TOKEN: syncToken,
      MATRIX_SYNC_SERVER_TIMEOUT_MS: "1000",
      MATRIX_SYNC_REQUEST_TIMEOUT_MS: "5000"
    });
    const deliveries: any[] = [];
    const ingestor = new MatrixSyncIngestor(
      "live-ingestor",
      client,
      attribution,
      rooms,
      checkpoints,
      fakeService(deliveries),
      { MATRIX_SYNC_USER_MXID: syncMxid }
    );

    const bootstrap = await ingestor.runOnce();
    expect(bootstrap.status).toBe("bootstrapped");
    expect(bootstrap.roomsJoined).toBe(1);

    await matrixRequest("PUT", `/_matrix/client/v3/rooms/${encodeURIComponent(roomId)}/send/m.room.message/live-human`, botToken, {
      msgtype: "m.text",
      body: "ordinary matrix message without bridge provenance"
    });
    await matrixRequest("PUT", `/_matrix/client/v3/rooms/${encodeURIComponent(roomId)}/send/m.room.message/live-meta-text`, botToken, {
      msgtype: "m.text",
      body: "hello from Meta",
      "com.mautrix_meta_stack.provenance": { source: "meta" },
      "com.mautrix_meta_stack.remote_sender_id": "2002"
    });
    await matrixRequest("PUT", `/_matrix/client/v3/rooms/${encodeURIComponent(roomId)}/send/m.room.message/live-meta-file`, botToken, {
      msgtype: "m.file",
      body: "document.pdf",
      url: "mxc://matrix.example.com/fake-document",
      info: { mimetype: "application/pdf", size: 1234 },
      "com.mautrix_meta_stack.provenance": { source: "meta" },
      "com.mautrix_meta_stack.remote_sender_id": "2002"
    });
    await matrixRequest("PUT", `/_matrix/client/v3/rooms/${encodeURIComponent(roomId)}/send/m.room.message/live-meta-voice`, botToken, {
      msgtype: "m.audio",
      body: "voice.ogg",
      url: "mxc://matrix.example.com/fake-voice",
      info: { mimetype: "audio/ogg", size: 4321 },
      "org.matrix.msc3245.voice": {},
      "com.mautrix_meta_stack.provenance": { source: "meta" },
      "com.mautrix_meta_stack.remote_sender_id": "2002"
    });

    const processed = await ingestor.runOnce();
    expect(processed.status).toBe("processed");
    expect(processed.eventsDelivered).toBe(3);
    expect(processed.eventsIgnored).toBeGreaterThanOrEqual(1);
    expect(deliveries).toHaveLength(3);
    expect(deliveries[0]).toMatchObject({
      connectionId: connection.id,
      roomId,
      remoteThreadId: "thread-live",
      remoteContactId: "2002",
      text: "hello from Meta"
    });
    expect(deliveries[1].attachments?.[0]).toMatchObject({
      kind: "file",
      mimeType: "application/pdf",
      fileName: "document.pdf",
      sizeBytes: 1234
    });
    expect(deliveries[2].attachments?.[0]).toMatchObject({
      kind: "audio",
      mimeType: "audio/ogg",
      fileName: "voice.ogg",
      sizeBytes: 4321,
      voiceNote: true
    });
    expect(rooms.findByRoomId(roomId)?.metaConnectionId).toBe(connection.id);
    expect(checkpoints.get("live-ingestor")?.nextBatch).toBe(processed.nextCheckpoint);
    database.close();

    const reopened = new Database(dbPath, { strict: true });
    runMigrations(reopened);
    expect(new SQLiteMatrixRoomBindingRepository(reopened).findByRoomId(roomId)?.remoteThreadId).toBe("thread-live");
    expect(new SQLiteMatrixSyncCheckpointRepository(reopened).get("live-ingestor")?.nextBatch).toBe(processed.nextCheckpoint);
    reopened.close();
  });
});
