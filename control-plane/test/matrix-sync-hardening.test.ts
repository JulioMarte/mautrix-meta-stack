import { afterEach, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import type { MatrixToChatwootService } from "../src/services/matrix-to-chatwoot";
import { runMigrations } from "../src/persistence/migrations";
import { SQLiteMatrixRoomBindingRepository, SQLiteMatrixSyncCheckpointRepository } from "../src/persistence/sqlite-matrix-repositories";
import { SQLiteMetaConnectionRepository, SQLiteTenantRepository } from "../src/persistence/sqlite-repositories";
import { MatrixRoomAttributionService } from "../src/services/matrix-room-attribution";
import { MatrixSyncIngestor } from "../src/services/matrix-sync-ingestor";
import type { MatrixSyncResponse } from "../src/services/matrix-sync-client";

let db: Database | undefined;
afterEach(() => { db?.close(); db = undefined; });

function setup() {
  db = new Database(":memory:", { strict: true });
  runMigrations(db);
  const tenants = new SQLiteTenantRepository(db);
  const connections = new SQLiteMetaConnectionRepository(db);
  const tenant = tenants.create({ slug: "hardening-a", name: "Hardening A" });
  const created = connections.create({ tenantId: tenant.id, matrixOwnerMxid: "@owner:matrix.example.com" });
  connections.setProviderIdentity(created.id, { metaAccountId: "1001", mautrixLoginId: "login-a" });
  connections.setStatus(created.id, "active");
  const rooms = new SQLiteMatrixRoomBindingRepository(db);
  const checkpoints = new SQLiteMatrixSyncCheckpointRepository(db);
  const attribution = new MatrixRoomAttributionService(connections, rooms, {
    MATRIX_BRIDGE_BOT_MXID: "@metabot:matrix.example.com",
    MATRIX_BRIDGE_PROTOCOL_IDS: "facebookgo"
  });
  return { rooms, checkpoints, attribution };
}

function bridgeState(receiver = "login-a") {
  return [{
    type: "m.bridge",
    event_id: "$bridge",
    state_key: "facebookgo://thread-a",
    sender: "@metabot:matrix.example.com",
    content: { protocol: { id: "facebookgo" }, channel: { id: "thread-a", receiver } }
  }];
}

function metaText(eventId: string, body = eventId) {
  return {
    type: "m.room.message",
    event_id: eventId,
    sender: "@facebook_2000:matrix.example.com",
    origin_server_ts: 1_700_000_000_000,
    content: {
      msgtype: "m.text",
      body,
      "com.mautrix_meta_stack.provenance": { source: "meta" },
      "com.mautrix_meta_stack.remote_sender_id": "2000"
    }
  };
}

function service(onHandle?: (event: any) => void): MatrixToChatwootService {
  return { async handle(event: any) { onHandle?.(event); return { status: "delivered", processedEventId: "p", conversationBindingId: "c", chatwootMessageId: "m" }; } } as unknown as MatrixToChatwootService;
}

describe("Matrix sync hardening", () => {
  test("limited timeline fails closed without recovery transport and without advancing checkpoint", async () => {
    const { rooms, checkpoints, attribution } = setup();
    checkpoints.save("ingestor", "s1");
    let deliveries = 0;
    const response: MatrixSyncResponse = {
      next_batch: "s2",
      rooms: { join: { "!room:matrix.example.com": { state: { events: bridgeState() }, timeline: { limited: true, prev_batch: "p1", events: [] } } }, invite: {} }
    };
    const ingestor = new MatrixSyncIngestor("ingestor", {
      async sync() { return response; }, async roomState() { return bridgeState(); }, async joinRoom() {}
    }, attribution, rooms, checkpoints, service(() => { deliveries++; }), { MATRIX_SYNC_USER_MXID: "@ingestor:matrix.example.com" });
    await expect(ingestor.runOnce()).rejects.toThrow("MATRIX_SYNC_GAP_RECOVERY_UNAVAILABLE");
    expect(checkpoints.get("ingestor")?.nextBatch).toBe("s1");
    expect(deliveries).toBe(0);
  });

  test("limited timeline without prev_batch fails closed before recovery and preserves checkpoint", async () => {
    const { rooms, checkpoints, attribution } = setup();
    checkpoints.save("ingestor", "s1");
    let recoveryCalls = 0;
    let deliveries = 0;
    const response: MatrixSyncResponse = {
      next_batch: "s2",
      rooms: { join: { "!room:matrix.example.com": { state: { events: bridgeState() }, timeline: { limited: true, events: [metaText("$current")] } } }, invite: {} }
    };
    const ingestor = new MatrixSyncIngestor("ingestor", {
      async sync() { return response; },
      async roomState() { return bridgeState(); },
      async joinRoom() {},
      async roomMessages() { recoveryCalls++; return { start: "s1", end: null, chunk: [] }; }
    }, attribution, rooms, checkpoints, service(() => { deliveries++; }), { MATRIX_SYNC_USER_MXID: "@ingestor:matrix.example.com" });
    await expect(ingestor.runOnce()).rejects.toThrow("MATRIX_SYNC_GAP_PREV_BATCH_REQUIRED");
    expect(recoveryCalls).toBe(0);
    expect(checkpoints.get("ingestor")?.nextBatch).toBe("s1");
    expect(deliveries).toBe(0);
  });

  test("gap recovery transport failure propagates and preserves checkpoint without side effects", async () => {
    const { rooms, checkpoints, attribution } = setup();
    checkpoints.save("ingestor", "s1");
    let deliveries = 0;
    const response: MatrixSyncResponse = {
      next_batch: "s2",
      rooms: { join: { "!room:matrix.example.com": { state: { events: bridgeState() }, timeline: { limited: true, prev_batch: "p1", events: [metaText("$current")] } } }, invite: {} }
    };
    const ingestor = new MatrixSyncIngestor("ingestor", {
      async sync() { return response; },
      async roomState() { return bridgeState(); },
      async joinRoom() {},
      async roomMessages() { throw new Error("MATRIX_SYNC_REQUEST_FAILED"); }
    }, attribution, rooms, checkpoints, service(() => { deliveries++; }), { MATRIX_SYNC_USER_MXID: "@ingestor:matrix.example.com" });
    await expect(ingestor.runOnce()).rejects.toThrow("MATRIX_SYNC_REQUEST_FAILED");
    expect(checkpoints.get("ingestor")?.nextBatch).toBe("s1");
    expect(deliveries).toBe(0);
  });

  test("recovers a limited timeline across pages, preserves order, and deduplicates overlap before advancing checkpoint", async () => {
    const { rooms, checkpoints, attribution } = setup();
    checkpoints.save("ingestor", "s1");
    const delivered: string[] = [];
    const ranges: Array<[string, string, string, number]> = [];
    const response: MatrixSyncResponse = {
      next_batch: "s2",
      rooms: { join: { "!room:matrix.example.com": {
        state: { events: bridgeState() },
        timeline: { limited: true, prev_batch: "p1", events: [metaText("$overlap"), metaText("$current")] }
      } }, invite: {} }
    };
    const ingestor = new MatrixSyncIngestor("ingestor", {
      async sync() { return response; },
      async roomState() { return bridgeState(); },
      async joinRoom() {},
      async roomMessages(roomId: string, from: string, to: string, limit: number) {
        ranges.push([roomId, from, to, limit]);
        if (from === "s1") return { start: "s1", end: "mid", chunk: [metaText("$missed-1"), metaText("$overlap")] };
        if (from === "mid") return { start: "mid", end: "p1", chunk: [metaText("$missed-2"), metaText("$overlap")] };
        throw new Error(`unexpected recovery cursor ${from}`);
      }
    }, attribution, rooms, checkpoints, service((event) => delivered.push(event.eventId)), { MATRIX_SYNC_USER_MXID: "@ingestor:matrix.example.com" });

    const result = await ingestor.runOnce();
    expect(result.eventsDelivered).toBe(4);
    expect(delivered).toEqual(["$missed-1", "$overlap", "$missed-2", "$current"]);
    expect(ranges).toEqual([
      ["!room:matrix.example.com", "s1", "p1", 100],
      ["!room:matrix.example.com", "mid", "p1", 100]
    ]);
    expect(checkpoints.get("ingestor")?.nextBatch).toBe("s2");
  });

  test("non-converging gap recovery fails closed and preserves checkpoint", async () => {
    const { rooms, checkpoints, attribution } = setup();
    checkpoints.save("ingestor", "s1");
    let deliveries = 0;
    const response: MatrixSyncResponse = {
      next_batch: "s2",
      rooms: { join: { "!room:matrix.example.com": {
        state: { events: bridgeState() },
        timeline: { limited: true, prev_batch: "p1", events: [metaText("$current")] }
      } }, invite: {} }
    };
    const ingestor = new MatrixSyncIngestor("ingestor", {
      async sync() { return response; },
      async roomState() { return bridgeState(); },
      async joinRoom() {},
      async roomMessages(_roomId: string, from: string) { return { start: from, end: from, chunk: [metaText("$missed")] }; }
    }, attribution, rooms, checkpoints, service(() => { deliveries++; }), { MATRIX_SYNC_USER_MXID: "@ingestor:matrix.example.com" });

    await expect(ingestor.runOnce()).rejects.toThrow("MATRIX_SYNC_GAP_NOT_CONVERGED");
    expect(checkpoints.get("ingestor")?.nextBatch).toBe("s1");
    expect(deliveries).toBe(0);
  });

  test("bounded gap recovery rejects oversized history before side effects and preserves checkpoint", async () => {
    const { rooms, checkpoints, attribution } = setup();
    checkpoints.save("ingestor", "s1");
    let deliveries = 0;
    const response: MatrixSyncResponse = {
      next_batch: "s2",
      rooms: { join: { "!room:matrix.example.com": {
        state: { events: bridgeState() },
        timeline: { limited: true, prev_batch: "p1", events: [] }
      } }, invite: {} }
    };
    const ingestor = new MatrixSyncIngestor("ingestor", {
      async sync() { return response; },
      async roomState() { return bridgeState(); },
      async joinRoom() {},
      async roomMessages() { return { start: "s1", end: "p1", chunk: [metaText("$one"), metaText("$two")] }; }
    }, attribution, rooms, checkpoints, service(() => { deliveries++; }), {
      MATRIX_SYNC_USER_MXID: "@ingestor:matrix.example.com",
      MATRIX_SYNC_MAX_GAP_EVENTS: "1"
    });

    await expect(ingestor.runOnce()).rejects.toThrow("MATRIX_SYNC_GAP_TOO_LARGE");
    expect(checkpoints.get("ingestor")?.nextBatch).toBe("s1");
    expect(deliveries).toBe(0);
  });

  test("bounded gap recovery stops after the configured page limit and preserves checkpoint", async () => {
    const { rooms, checkpoints, attribution } = setup();
    checkpoints.save("ingestor", "s1");
    let recoveryCalls = 0;
    let deliveries = 0;
    const response: MatrixSyncResponse = {
      next_batch: "s2",
      rooms: { join: { "!room:matrix.example.com": {
        state: { events: bridgeState() },
        timeline: { limited: true, prev_batch: "p1", events: [metaText("$current")] }
      } }, invite: {} }
    };
    const ingestor = new MatrixSyncIngestor("ingestor", {
      async sync() { return response; },
      async roomState() { return bridgeState(); },
      async joinRoom() {},
      async roomMessages(_roomId: string, from: string) {
        recoveryCalls++;
        return { start: from, end: "mid", chunk: [metaText("$missed")] };
      }
    }, attribution, rooms, checkpoints, service(() => { deliveries++; }), {
      MATRIX_SYNC_USER_MXID: "@ingestor:matrix.example.com",
      MATRIX_SYNC_MAX_GAP_PAGES: "1"
    });

    await expect(ingestor.runOnce()).rejects.toThrow("MATRIX_SYNC_GAP_TOO_LARGE");
    expect(recoveryCalls).toBe(1);
    expect(checkpoints.get("ingestor")?.nextBatch).toBe("s1");
    expect(deliveries).toBe(0);
  });

  test("unknown bridge login is ignored without side effects or blocking checkpoint advancement", async () => {
    const { rooms, checkpoints, attribution } = setup();
    checkpoints.save("ingestor", "s1");
    let deliveries = 0;
    const unknownState = bridgeState("login-does-not-exist");
    const response: MatrixSyncResponse = {
      next_batch: "s2",
      rooms: { join: { "!unknown:matrix.example.com": {
        state: { events: unknownState },
        timeline: { events: [metaText("$unknown", "must not route")] }
      } }, invite: {} }
    };
    const ingestor = new MatrixSyncIngestor("ingestor", {
      async sync() { return response; }, async roomState() { return unknownState; }, async joinRoom() {}
    }, attribution, rooms, checkpoints, service(() => { deliveries++; }), { MATRIX_SYNC_USER_MXID: "@ingestor:matrix.example.com" });
    const result = await ingestor.runOnce();
    expect(result.status).toBe("processed");
    expect(result.eventsDelivered).toBe(0);
    expect(result.eventsIgnored).toBe(1);
    expect(checkpoints.get("ingestor")?.nextBatch).toBe("s2");
    expect(rooms.findByRoomId("!unknown:matrix.example.com")).toBeNull();
    expect(deliveries).toBe(0);
  });

  test("encrypted media without decrypt key operation fails closed and preserves checkpoint", async () => {
    const { rooms, checkpoints, attribution } = setup();
    checkpoints.save("ingestor", "s1");
    let deliveries = 0;
    const response: MatrixSyncResponse = {
      next_batch: "s2",
      rooms: { join: { "!room:matrix.example.com": {
        state: { events: bridgeState() },
        timeline: { events: [{
          type: "m.room.message",
          event_id: "$bad-encrypted",
          sender: "@facebook_2000:matrix.example.com",
          origin_server_ts: 1_700_000_000_000,
          content: {
            msgtype: "m.file",
            body: "secret.pdf",
            file: {
              v: "v2",
              url: "mxc://matrix.example.com/ciphertext",
              key: { kty: "oct", alg: "A256CTR", k: "ZmFrZS1rZXk", key_ops: ["encrypt"], ext: true },
              iv: "ZmFrZS1pdg",
              hashes: { sha256: "ZmFrZS1oYXNo" }
            },
            "com.mautrix_meta_stack.provenance": { source: "meta" },
            "com.mautrix_meta_stack.remote_sender_id": "2000"
          }
        }] }
      } }, invite: {} }
    };
    const ingestor = new MatrixSyncIngestor("ingestor", {
      async sync() { return response; }, async roomState() { return bridgeState(); }, async joinRoom() {}
    }, attribution, rooms, checkpoints, service(() => { deliveries++; }), { MATRIX_SYNC_USER_MXID: "@ingestor:matrix.example.com" });
    await expect(ingestor.runOnce()).rejects.toThrow("MATRIX_MEDIA_ENCRYPTION_INVALID");
    expect(checkpoints.get("ingestor")?.nextBatch).toBe("s1");
    expect(deliveries).toBe(0);
  });
});
