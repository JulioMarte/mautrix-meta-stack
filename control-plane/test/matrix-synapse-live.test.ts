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

async function createBridgeRoom(remoteThreadId: string, loginId: string): Promise<string> {
  const created = await matrixRequest("POST", "/_matrix/client/v3/createRoom", botToken, {
    preset: "private_chat",
    invite: [syncMxid],
    initial_state: [{
      type: "m.bridge",
      state_key: `facebookgo://${remoteThreadId}`,
      content: {
        protocol: { id: "facebookgo" },
        channel: { id: remoteThreadId, receiver: loginId }
      }
    }]
  });
  if (typeof created?.room_id !== "string") throw new Error("live room creation did not return room_id");
  return created.room_id;
}

async function sendMessage(roomId: string, txn: string, content: Record<string, unknown>) {
  return matrixRequest("PUT", `/_matrix/client/v3/rooms/${encodeURIComponent(roomId)}/send/m.room.message/${txn}`, botToken, content);
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
  test("auth, trusted joins, two-tenant attribution, attachments and unbound-room rejection work against Synapse", async () => {
    const badClient = new HttpMatrixSyncClient({
      MATRIX_SYNC_BASE_URL: base,
      MATRIX_SYNC_ACCESS_TOKEN: "definitely-invalid-token",
      MATRIX_SYNC_SERVER_TIMEOUT_MS: "1000",
      MATRIX_SYNC_REQUEST_TIMEOUT_MS: "5000"
    });
    await expect(badClient.sync(null, { timelineLimit: 0 })).rejects.toThrow("MATRIX_SYNC_UNAUTHORIZED");

    const roomA = await createBridgeRoom("thread-a", "login-a");
    const roomB = await createBridgeRoom("thread-b", "login-b");
    const roomUnbound = await createBridgeRoom("thread-unbound", "login-does-not-exist");

    const dbPath = join(tempDir, "control-plane.db");
    const database = new Database(dbPath, { create: true, strict: true });
    runMigrations(database);
    const tenants = new SQLiteTenantRepository(database);
    const connections = new SQLiteMetaConnectionRepository(database);
    const tenantA = tenants.create({ slug: "live-a", name: "Live A" });
    const tenantB = tenants.create({ slug: "live-b", name: "Live B" });
    const connectionA = connections.create({ tenantId: tenantA.id, matrixOwnerMxid: "@owner-a:matrix.example.com" });
    const connectionB = connections.create({ tenantId: tenantB.id, matrixOwnerMxid: "@owner-b:matrix.example.com" });
    connections.setProviderIdentity(connectionA.id, { metaAccountId: "1001", mautrixLoginId: "login-a" });
    connections.setProviderIdentity(connectionB.id, { metaAccountId: "1002", mautrixLoginId: "login-b" });
    connections.setStatus(connectionA.id, "active");
    connections.setStatus(connectionB.id, "active");

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
    expect(bootstrap.roomsJoined).toBe(3);

    await sendMessage(roomA, "human-a", {
      msgtype: "m.text",
      body: "ordinary Matrix message"
    });
    await sendMessage(roomA, "meta-a-text", {
      msgtype: "m.text",
      body: "hello A",
      "com.mautrix_meta_stack.provenance": { source: "meta" },
      "com.mautrix_meta_stack.remote_sender_id": "2000"
    });
    await sendMessage(roomA, "meta-a-file", {
      msgtype: "m.file",
      body: "document.pdf",
      url: "mxc://matrix.example.com/fake-document",
      info: { mimetype: "application/pdf", size: 1234 },
      "com.mautrix_meta_stack.provenance": { source: "meta" },
      "com.mautrix_meta_stack.remote_sender_id": "2000"
    });
    await sendMessage(roomA, "meta-a-voice", {
      msgtype: "m.audio",
      body: "voice.ogg",
      url: "mxc://matrix.example.com/fake-voice",
      info: { mimetype: "audio/ogg", size: 4321 },
      "org.matrix.msc3245.voice": {},
      "com.mautrix_meta_stack.provenance": { source: "meta" },
      "com.mautrix_meta_stack.remote_sender_id": "2000"
    });
    await sendMessage(roomB, "meta-b-text", {
      msgtype: "m.text",
      body: "hello B",
      "com.mautrix_meta_stack.provenance": { source: "meta" },
      "com.mautrix_meta_stack.remote_sender_id": "2000"
    });
    await sendMessage(roomUnbound, "meta-unbound", {
      msgtype: "m.text",
      body: "must not route",
      "com.mautrix_meta_stack.provenance": { source: "meta" },
      "com.mautrix_meta_stack.remote_sender_id": "2999"
    });

    const processed = await ingestor.runOnce();
    expect(processed.status).toBe("processed");
    expect(processed.eventsDelivered).toBe(4);
    expect(processed.eventsIgnored).toBeGreaterThanOrEqual(2);
    expect(deliveries).toHaveLength(4);

    const aText = deliveries.find((event) => event.text === "hello A");
    const bText = deliveries.find((event) => event.text === "hello B");
    expect(aText).toMatchObject({
      connectionId: connectionA.id,
      roomId: roomA,
      remoteThreadId: "thread-a",
      remoteContactId: "2000"
    });
    expect(bText).toMatchObject({
      connectionId: connectionB.id,
      roomId: roomB,
      remoteThreadId: "thread-b",
      remoteContactId: "2000"
    });
    expect(aText.connectionId).not.toBe(bText.connectionId);
    expect(deliveries.some((event) => event.roomId === roomUnbound)).toBe(false);

    const pdf = deliveries.find((event) => event.attachments?.[0]?.fileName === "document.pdf");
    expect(pdf?.attachments?.[0]).toMatchObject({
      kind: "file",
      mimeType: "application/pdf",
      sizeBytes: 1234
    });
    const voice = deliveries.find((event) => event.attachments?.[0]?.voiceNote === true);
    expect(voice?.attachments?.[0]).toMatchObject({
      kind: "audio",
      mimeType: "audio/ogg",
      fileName: "voice.ogg",
      sizeBytes: 4321,
      voiceNote: true
    });

    expect(rooms.findByRoomId(roomA)?.metaConnectionId).toBe(connectionA.id);
    expect(rooms.findByRoomId(roomB)?.metaConnectionId).toBe(connectionB.id);
    expect(rooms.findByRoomId(roomUnbound)).toBeNull();
    expect(checkpoints.get("live-ingestor")?.nextBatch).toBe(processed.nextCheckpoint);
    database.close();

    const reopened = new Database(dbPath, { strict: true });
    runMigrations(reopened);
    const reopenedRooms = new SQLiteMatrixRoomBindingRepository(reopened);
    expect(reopenedRooms.findByRoomId(roomA)?.remoteThreadId).toBe("thread-a");
    expect(reopenedRooms.findByRoomId(roomB)?.remoteThreadId).toBe("thread-b");
    expect(reopenedRooms.findByRoomId(roomUnbound)).toBeNull();
    expect(new SQLiteMatrixSyncCheckpointRepository(reopened).get("live-ingestor")?.nextBatch).toBe(processed.nextCheckpoint);
    reopened.close();
  });
});
