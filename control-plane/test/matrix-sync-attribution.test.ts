import { afterEach, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { runMigrations } from "../src/persistence/migrations";
import { SQLiteMatrixRoomBindingRepository } from "../src/persistence/sqlite-matrix-repositories";
import { SQLiteMetaConnectionRepository, SQLiteTenantRepository } from "../src/persistence/sqlite-repositories";
import { MatrixRoomAttributionService } from "../src/services/matrix-room-attribution";
import { HttpMatrixSyncClient } from "../src/services/matrix-sync-client";

let db: Database | undefined;
afterEach(() => { db?.close(); db = undefined; });

function freshDb(): Database {
  db = new Database(":memory:", { strict: true });
  runMigrations(db);
  return db;
}

function makeActiveConnection(database: Database, loginId = "login-a") {
  const tenants = new SQLiteTenantRepository(database);
  const connections = new SQLiteMetaConnectionRepository(database);
  const tenant = tenants.create({ slug: "tenant-a", name: "Tenant A" });
  const created = connections.create({ tenantId: tenant.id, matrixOwnerMxid: "@owner:matrix.example.com" });
  connections.setProviderIdentity(created.id, { metaAccountId: "1001", mautrixLoginId: loginId });
  connections.setStatus(created.id, "active");
  return { tenant, connection: connections.findById(created.id)!, connections };
}

describe("Matrix sync client", () => {
  test("uses bearer auth, preserves checkpoint and parses bounded join data", async () => {
    let seen: URL | undefined;
    let authorization: string | null = null;
    const client = new HttpMatrixSyncClient({
      MATRIX_SYNC_BASE_URL: "http://synapse:8008",
      MATRIX_SYNC_ACCESS_TOKEN: "sync-secret",
      MATRIX_SYNC_SERVER_TIMEOUT_MS: "1000",
      MATRIX_SYNC_REQUEST_TIMEOUT_MS: "2000"
    }, async (input, init) => {
      seen = new URL(String(input));
      authorization = new Headers(init?.headers).get("authorization");
      return Response.json({
        next_batch: "s124",
        rooms: { join: {
          "!room:matrix.example.com": {
            state: { events: [{ type: "m.bridge", state_key: "bridge", sender: "@metabot:matrix.example.com", content: {} }] },
            timeline: { events: [{ type: "m.room.message", event_id: "$event", content: { msgtype: "m.text", body: "hello" } }] }
          }
        } }
      });
    });
    const result = await client.sync("s123");
    expect(authorization).toBe("Bearer sync-secret");
    expect(seen?.searchParams.get("since")).toBe("s123");
    expect(seen?.searchParams.get("timeout")).toBe("1000");
    expect(seen?.searchParams.has("access_token")).toBe(false);
    expect(result.next_batch).toBe("s124");
    expect(result.rooms.join["!room:matrix.example.com"]?.timeline?.events).toHaveLength(1);
  });

  test("maps unauthorized responses to a stable error", async () => {
    const client = new HttpMatrixSyncClient({
      MATRIX_SYNC_BASE_URL: "http://synapse:8008",
      MATRIX_SYNC_ACCESS_TOKEN: "bad-token",
      MATRIX_SYNC_SERVER_TIMEOUT_MS: "1000",
      MATRIX_SYNC_REQUEST_TIMEOUT_MS: "2000"
    }, async () => new Response("{}", { status: 401 }));
    expect(client.sync(null)).rejects.toThrow("MATRIX_SYNC_UNAUTHORIZED");
  });

  test("rejects malformed sync responses and invalid timeout configuration", async () => {
    const client = new HttpMatrixSyncClient({
      MATRIX_SYNC_BASE_URL: "http://synapse:8008",
      MATRIX_SYNC_ACCESS_TOKEN: "token",
      MATRIX_SYNC_SERVER_TIMEOUT_MS: "1000",
      MATRIX_SYNC_REQUEST_TIMEOUT_MS: "2000"
    }, async () => Response.json({ rooms: {} }));
    expect(client.sync(null)).rejects.toThrow("MATRIX_SYNC_RESPONSE_INVALID");
    expect(() => new HttpMatrixSyncClient({
      MATRIX_SYNC_BASE_URL: "http://synapse:8008",
      MATRIX_SYNC_ACCESS_TOKEN: "token",
      MATRIX_SYNC_SERVER_TIMEOUT_MS: "2000",
      MATRIX_SYNC_REQUEST_TIMEOUT_MS: "1000"
    })).toThrow("MATRIX_SYNC_TIMEOUT_CONFIGURATION_INVALID");
  });
});

describe("Matrix bridge room attribution", () => {
  test("binds m.bridge channel receiver to the unique active connection", () => {
    const database = freshDb();
    const { tenant, connection, connections } = makeActiveConnection(database);
    const rooms = new SQLiteMatrixRoomBindingRepository(database);
    const service = new MatrixRoomAttributionService(connections, rooms, {
      MATRIX_BRIDGE_BOT_MXID: "@metabot:matrix.example.com",
      MATRIX_BRIDGE_PROTOCOL_IDS: "facebookgo"
    });
    const binding = service.bindRoom("!room:matrix.example.com", [{
      type: "m.bridge",
      event_id: "$bridge",
      state_key: "com.example.meta",
      sender: "@metabot:matrix.example.com",
      content: {
        protocol: { id: "facebookgo" },
        channel: { id: "thread-123", receiver: "login-a" }
      }
    }]);
    expect(binding.tenantId).toBe(tenant.id);
    expect(binding.metaConnectionId).toBe(connection.id);
    expect(binding.remoteThreadId).toBe("thread-123");
    expect(binding.mautrixLoginId).toBe("login-a");
  });

  test("wrong sender or protocol cannot establish attribution", () => {
    const database = freshDb();
    const { connections } = makeActiveConnection(database);
    const rooms = new SQLiteMatrixRoomBindingRepository(database);
    const service = new MatrixRoomAttributionService(connections, rooms, {
      MATRIX_BRIDGE_BOT_MXID: "@metabot:matrix.example.com",
      MATRIX_BRIDGE_PROTOCOL_IDS: "facebookgo"
    });
    const base = {
      type: "m.bridge",
      state_key: "bridge",
      content: { protocol: { id: "facebookgo" }, channel: { id: "thread-1", receiver: "login-a" } }
    };
    expect(() => service.bindRoom("!wrong-sender:matrix.example.com", [{ ...base, sender: "@user:matrix.example.com" }])).toThrow("MATRIX_BRIDGE_STATE_NOT_FOUND");
    expect(() => service.bindRoom("!wrong-protocol:matrix.example.com", [{
      ...base,
      sender: "@metabot:matrix.example.com",
      content: { protocol: { id: "instagramgo" }, channel: { id: "thread-1", receiver: "login-a" } }
    }])).toThrow("MATRIX_BRIDGE_STATE_NOT_FOUND");
  });

  test("conflicting bridge state fails closed", () => {
    const database = freshDb();
    const { connections } = makeActiveConnection(database);
    const rooms = new SQLiteMatrixRoomBindingRepository(database);
    const service = new MatrixRoomAttributionService(connections, rooms, {
      MATRIX_BRIDGE_BOT_MXID: "@metabot:matrix.example.com"
    });
    expect(() => service.bindRoom("!ambiguous:matrix.example.com", [
      {
        type: "m.bridge", state_key: "bridge", sender: "@metabot:matrix.example.com",
        content: { protocol: { id: "facebookgo" }, channel: { id: "thread-a", receiver: "login-a" } }
      },
      {
        type: "uk.half-shot.bridge", state_key: "bridge", sender: "@metabot:matrix.example.com",
        content: { protocol: { id: "facebookgo" }, channel: { id: "thread-b", receiver: "login-a" } }
      }
    ])).toThrow("MATRIX_BRIDGE_STATE_AMBIGUOUS");
  });
});
