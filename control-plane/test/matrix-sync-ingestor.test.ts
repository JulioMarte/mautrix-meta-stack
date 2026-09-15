import { afterEach, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import type { MatrixToChatwootService } from "../src/services/matrix-to-chatwoot";
import { runMigrations } from "../src/persistence/migrations";
import {
  SQLiteMatrixRoomBindingRepository,
  SQLiteMatrixSyncCheckpointRepository
} from "../src/persistence/sqlite-matrix-repositories";
import { SQLiteMetaConnectionRepository, SQLiteTenantRepository } from "../src/persistence/sqlite-repositories";
import { MatrixRoomAttributionService } from "../src/services/matrix-room-attribution";
import { MatrixSyncIngestor } from "../src/services/matrix-sync-ingestor";
import type { MatrixSyncResponse } from "../src/services/matrix-sync-client";

let db: Database | undefined;
afterEach(() => { db?.close(); db = undefined; });

function freshDb(): Database {
  db = new Database(":memory:", { strict: true });
  runMigrations(db);
  return db;
}

function setup() {
  const database = freshDb();
  const tenants = new SQLiteTenantRepository(database);
  const connections = new SQLiteMetaConnectionRepository(database);
  const tenant = tenants.create({ slug: "tenant-a", name: "Tenant A" });
  const created = connections.create({ tenantId: tenant.id, matrixOwnerMxid: "@owner:matrix.example.com" });
  connections.setProviderIdentity(created.id, { metaAccountId: "1001", mautrixLoginId: "login-a" });
  connections.setStatus(created.id, "active");
  const roomBindings = new SQLiteMatrixRoomBindingRepository(database);
  const checkpoints = new SQLiteMatrixSyncCheckpointRepository(database);
  const attribution = new MatrixRoomAttributionService(connections, roomBindings, {
    MATRIX_BRIDGE_BOT_MXID: "@metabot:matrix.example.com",
    MATRIX_BRIDGE_PROTOCOL_IDS: "facebookgo"
  });
  return { database, connection: connections.findById(created.id)!, roomBindings, checkpoints, attribution };
}

function bridgeState() {
  return [{
    type: "m.bridge",
    event_id: "$bridge",
    state_key: "facebookgo://thread-1",
    sender: "@metabot:matrix.example.com",
    content: {
      protocol: { id: "facebookgo" },
      channel: { id: "thread-1", receiver: "login-a" }
    }
  }];
}

function fakeService(onHandle?: (event: any) => void): MatrixToChatwootService {
  return {
    async handle(event: any) {
      onHandle?.(event);
      return { status: "delivered", processedEventId: "p", conversationBindingId: "c", chatwootMessageId: "m" };
    }
  } as unknown as MatrixToChatwootService;
}

function emptySync(nextBatch: string): MatrixSyncResponse {
  return { next_batch: nextBatch, rooms: { join: {}, invite: {} } };
}

describe("Matrix sync invite and provenance boundaries", () => {
  test("joins exactly trusted bridge-bot invitations during bootstrap", async () => {
    const { roomBindings, checkpoints, attribution } = setup();
    const joined: string[] = [];
    const response = emptySync("s1");
    response.rooms.invite["!trusted:matrix.example.com"] = {
      invite_state: { events: [{
        type: "m.room.member",
        state_key: "@ingestor:matrix.example.com",
        sender: "@metabot:matrix.example.com",
        content: { membership: "invite" }
      }] }
    };
    const transport = {
      async sync() { return response; },
      async roomState() { return []; },
      async joinRoom(roomId: string) { joined.push(roomId); }
    };
    const ingestor = new MatrixSyncIngestor(
      "matrix-ingestor",
      transport,
      attribution,
      roomBindings,
      checkpoints,
      fakeService(),
      { MATRIX_SYNC_USER_MXID: "@ingestor:matrix.example.com" }
    );
    const result = await ingestor.runOnce();
    expect(result.status).toBe("bootstrapped");
    expect(result.roomsJoined).toBe(1);
    expect(joined).toEqual(["!trusted:matrix.example.com"]);
    expect(checkpoints.get("matrix-ingestor")?.nextBatch).toBe("s1");
  });

  test("ignores invitations not sent by the configured bridge bot", async () => {
    const { roomBindings, checkpoints, attribution } = setup();
    let joined = false;
    const response = emptySync("s1");
    response.rooms.invite["!untrusted:matrix.example.com"] = {
      invite_state: { events: [{
        type: "m.room.member",
        state_key: "@ingestor:matrix.example.com",
        sender: "@attacker:matrix.example.com",
        content: { membership: "invite" }
      }] }
    };
    const ingestor = new MatrixSyncIngestor(
      "matrix-ingestor",
      {
        async sync() { return response; },
        async roomState() { return []; },
        async joinRoom() { joined = true; }
      },
      attribution,
      roomBindings,
      checkpoints,
      fakeService(),
      { MATRIX_SYNC_USER_MXID: "@ingestor:matrix.example.com" }
    );
    const result = await ingestor.runOnce();
    expect(result.roomsJoined).toBe(0);
    expect(joined).toBe(false);
  });

  test("ordinary Matrix messages are ignored and cannot block checkpoint advancement", async () => {
    const { roomBindings, checkpoints, attribution } = setup();
    checkpoints.save("matrix-ingestor", "s1");
    let deliveries = 0;
    const response = emptySync("s2");
    response.rooms.join["!portal:matrix.example.com"] = {
      state: { events: bridgeState() },
      timeline: { events: [{
        type: "m.room.message",
        event_id: "$human",
        sender: "@human:matrix.example.com",
        origin_server_ts: 1_700_000_000_000,
        content: { msgtype: "m.text", body: "not a bridged Meta event" }
      }] }
    };
    const ingestor = new MatrixSyncIngestor(
      "matrix-ingestor",
      {
        async sync() { return response; },
        async roomState() { return bridgeState(); },
        async joinRoom() {}
      },
      attribution,
      roomBindings,
      checkpoints,
      fakeService(() => { deliveries++; }),
      { MATRIX_SYNC_USER_MXID: "@ingestor:matrix.example.com" }
    );
    const result = await ingestor.runOnce();
    expect(result.eventsIgnored).toBe(1);
    expect(result.eventsDelivered).toBe(0);
    expect(deliveries).toBe(0);
    expect(checkpoints.get("matrix-ingestor")?.nextBatch).toBe("s2");
  });

  test("Meta provenance without remote sender fails closed and preserves checkpoint", async () => {
    const { roomBindings, checkpoints, attribution } = setup();
    checkpoints.save("matrix-ingestor", "s1");
    const response = emptySync("s2");
    response.rooms.join["!portal:matrix.example.com"] = {
      state: { events: bridgeState() },
      timeline: { events: [{
        type: "m.room.message",
        event_id: "$broken-meta",
        sender: "@facebook_1002:matrix.example.com",
        origin_server_ts: 1_700_000_000_000,
        content: {
          msgtype: "m.text",
          body: "hello",
          "com.mautrix_meta_stack.provenance": { source: "meta" }
        }
      }] }
    };
    const ingestor = new MatrixSyncIngestor(
      "matrix-ingestor",
      {
        async sync() { return response; },
        async roomState() { return bridgeState(); },
        async joinRoom() {}
      },
      attribution,
      roomBindings,
      checkpoints,
      fakeService(),
      { MATRIX_SYNC_USER_MXID: "@ingestor:matrix.example.com" }
    );
    await expect(ingestor.runOnce()).rejects.toThrow("MATRIX_REMOTE_SENDER_ID_REQUIRED");
    expect(checkpoints.get("matrix-ingestor")?.nextBatch).toBe("s1");
  });

  test("explicit Meta provenance and remote sender are delivered", async () => {
    const { roomBindings, checkpoints, attribution, connection } = setup();
    checkpoints.save("matrix-ingestor", "s1");
    let delivered: any;
    const response = emptySync("s2");
    response.rooms.join["!portal:matrix.example.com"] = {
      state: { events: bridgeState() },
      timeline: { events: [{
        type: "m.room.message",
        event_id: "$meta",
        sender: "@facebook_1002:matrix.example.com",
        origin_server_ts: 1_700_000_000_000,
        content: {
          msgtype: "m.text",
          body: "hello",
          "com.mautrix_meta_stack.provenance": { source: "meta" },
          "com.mautrix_meta_stack.remote_sender_id": "1002"
        }
      }] }
    };
    const ingestor = new MatrixSyncIngestor(
      "matrix-ingestor",
      {
        async sync() { return response; },
        async roomState() { return bridgeState(); },
        async joinRoom() {}
      },
      attribution,
      roomBindings,
      checkpoints,
      fakeService((event) => { delivered = event; }),
      { MATRIX_SYNC_USER_MXID: "@ingestor:matrix.example.com" }
    );
    const result = await ingestor.runOnce();
    expect(result.eventsDelivered).toBe(1);
    expect(delivered.connectionId).toBe(connection.id);
    expect(delivered.remoteContactId).toBe("1002");
    expect(delivered.remoteThreadId).toBe("thread-1");
    expect(checkpoints.get("matrix-ingestor")?.nextBatch).toBe("s2");
  });
});
