import { afterEach, describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Database } from "bun:sqlite";
import { runMigrations, schemaVersion } from "../src/persistence/migrations";
import {
  SQLiteMatrixRoomBindingRepository,
  SQLiteMatrixSyncCheckpointRepository
} from "../src/persistence/sqlite-matrix-repositories";
import { SQLiteMetaConnectionRepository, SQLiteTenantRepository } from "../src/persistence/sqlite-repositories";

let db: Database | undefined;
let tempDir: string | undefined;
afterEach(() => {
  db?.close();
  db = undefined;
  if (tempDir) rmSync(tempDir, { recursive: true, force: true });
  tempDir = undefined;
});

function freshDb(): Database {
  db = new Database(":memory:", { strict: true });
  runMigrations(db);
  return db;
}

function activeConnection(database: Database, slug: string, loginId: string) {
  const tenants = new SQLiteTenantRepository(database);
  const connections = new SQLiteMetaConnectionRepository(database);
  const tenant = tenants.create({ slug, name: slug });
  const connection = connections.create({ tenantId: tenant.id, matrixOwnerMxid: `@${slug}:matrix.example.com` });
  connections.setProviderIdentity(connection.id, { metaAccountId: `${slug}-meta`, mautrixLoginId: loginId });
  connections.setStatus(connection.id, "active");
  return { tenant, connection: connections.findById(connection.id)! };
}

describe("Matrix ingestion persistence", () => {
  test("schema v4 creates Matrix room bindings and checkpoints", () => {
    const database = freshDb();
    expect(schemaVersion(database)).toBe(4);
    expect(database.query("SELECT name FROM sqlite_master WHERE type='table' AND name='matrix_room_bindings'").get()).not.toBeNull();
    expect(database.query("SELECT name FROM sqlite_master WHERE type='table' AND name='matrix_sync_checkpoints'").get()).not.toBeNull();
  });

  test("verified room attribution is idempotent but conflicting attribution fails closed", () => {
    const database = freshDb();
    const rooms = new SQLiteMatrixRoomBindingRepository(database);
    const a = activeConnection(database, "tenant-a", "login-a");
    const b = activeConnection(database, "tenant-b", "login-b");

    const first = rooms.bindVerified({
      matrixRoomId: "!room-a:matrix.example.com",
      tenantId: a.tenant.id,
      metaConnectionId: a.connection.id,
      remoteThreadId: "thread-a",
      mautrixLoginId: "login-a",
      bridgeStateKey: "facebook",
      sourceEventId: "$bridge-1"
    });
    expect(first.metaConnectionId).toBe(a.connection.id);

    const repeated = rooms.bindVerified({
      matrixRoomId: "!room-a:matrix.example.com",
      tenantId: a.tenant.id,
      metaConnectionId: a.connection.id,
      remoteThreadId: "thread-a",
      mautrixLoginId: "login-a",
      bridgeStateKey: "facebook",
      sourceEventId: "$bridge-2"
    });
    expect(repeated.sourceEventId).toBe("$bridge-2");
    expect(rooms.list()).toHaveLength(1);

    expect(() => rooms.bindVerified({
      matrixRoomId: "!room-a:matrix.example.com",
      tenantId: b.tenant.id,
      metaConnectionId: b.connection.id,
      remoteThreadId: "thread-b",
      mautrixLoginId: "login-b",
      bridgeStateKey: "facebook",
      sourceEventId: "$bridge-conflict"
    })).toThrow("MATRIX_ROOM_ATTRIBUTION_CONFLICT");

    expect(() => rooms.bindVerified({
      matrixRoomId: "!room-other:matrix.example.com",
      tenantId: a.tenant.id,
      metaConnectionId: a.connection.id,
      remoteThreadId: "thread-a",
      mautrixLoginId: "login-a",
      bridgeStateKey: "facebook",
      sourceEventId: "$bridge-thread-conflict"
    })).toThrow("MATRIX_REMOTE_THREAD_ROOM_CONFLICT");
  });

  test("binding rejects tenant and login identity mismatches", () => {
    const database = freshDb();
    const rooms = new SQLiteMatrixRoomBindingRepository(database);
    const a = activeConnection(database, "tenant-a", "login-a");
    const b = activeConnection(database, "tenant-b", "login-b");

    expect(() => rooms.bindVerified({
      matrixRoomId: "!cross:matrix.example.com",
      tenantId: b.tenant.id,
      metaConnectionId: a.connection.id,
      remoteThreadId: "thread-cross",
      mautrixLoginId: "login-a",
      bridgeStateKey: "facebook",
      sourceEventId: null
    })).toThrow("CROSS_TENANT_MATRIX_ROOM_BINDING");

    expect(() => rooms.bindVerified({
      matrixRoomId: "!wrong-login:matrix.example.com",
      tenantId: a.tenant.id,
      metaConnectionId: a.connection.id,
      remoteThreadId: "thread-wrong-login",
      mautrixLoginId: "login-b",
      bridgeStateKey: "facebook",
      sourceEventId: null
    })).toThrow("MATRIX_LOGIN_IDENTITY_CONFLICT");
  });

  test("sync checkpoint survives database reopen and advances atomically", () => {
    tempDir = mkdtempSync(join(tmpdir(), "matrix-sync-"));
    const dbPath = join(tempDir, "control-plane.db");
    let firstDb = new Database(dbPath, { create: true, strict: true });
    runMigrations(firstDb);
    const first = new SQLiteMatrixSyncCheckpointRepository(firstDb);
    expect(first.get("matrix-ingestor")).toBeNull();
    first.save("matrix-ingestor", "s123");
    firstDb.close();

    firstDb = new Database(dbPath, { strict: true });
    runMigrations(firstDb);
    const reopened = new SQLiteMatrixSyncCheckpointRepository(firstDb);
    expect(reopened.get("matrix-ingestor")?.nextBatch).toBe("s123");
    reopened.save("matrix-ingestor", "s124");
    expect(reopened.get("matrix-ingestor")?.nextBatch).toBe("s124");
    firstDb.close();
  });
});
