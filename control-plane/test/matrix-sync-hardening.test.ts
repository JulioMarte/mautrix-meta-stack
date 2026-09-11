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

function bridgeState() {
  return [{
    type: "m.bridge",
    event_id: "$bridge",
    state_key: "facebookgo://thread-a",
    sender: "@metabot:matrix.example.com",
    content: { protocol: { id: "facebookgo" }, channel: { id: "thread-a", receiver: "login-a" } }
  }];
}

function service(onHandle?: () => void): MatrixToChatwootService {
  return { async handle() { onHandle?.(); return { status: "delivered", processedEventId: "p", conversationBindingId: "c", chatwootMessageId: "m" }; } } as unknown as MatrixToChatwootService;
}

describe("Matrix sync hardening", () => {
  test("limited timeline fails closed without advancing checkpoint", async () => {
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
    await expect(ingestor.runOnce()).rejects.toThrow("MATRIX_SYNC_TIMELINE_GAP");
    expect(checkpoints.get("ingestor")?.nextBatch).toBe("s1");
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
