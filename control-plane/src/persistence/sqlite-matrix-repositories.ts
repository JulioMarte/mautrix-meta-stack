import type { Database } from "bun:sqlite";
import type {
  MatrixRoomBinding,
  MatrixRoomBindingRepository,
  MatrixSyncCheckpoint,
  MatrixSyncCheckpointRepository
} from "../domain/models";

function roomBindingFromRow(row: Record<string, unknown>): MatrixRoomBinding {
  return {
    matrixRoomId: String(row.matrix_room_id),
    tenantId: String(row.tenant_id),
    metaConnectionId: String(row.meta_connection_id),
    remoteThreadId: String(row.remote_thread_id),
    mautrixLoginId: String(row.mautrix_login_id),
    bridgeStateKey: String(row.bridge_state_key),
    sourceEventId: row.source_event_id == null ? null : String(row.source_event_id),
    verifiedAt: String(row.verified_at),
    createdAt: String(row.created_at),
    updatedAt: String(row.updated_at)
  };
}

function checkpointFromRow(row: Record<string, unknown>): MatrixSyncCheckpoint {
  return {
    consumerId: String(row.consumer_id),
    nextBatch: String(row.next_batch),
    updatedAt: String(row.updated_at)
  };
}

export class SQLiteMatrixRoomBindingRepository implements MatrixRoomBindingRepository {
  constructor(private readonly db: Database) {}

  bindVerified(input: Omit<MatrixRoomBinding, "verifiedAt" | "createdAt" | "updatedAt">): MatrixRoomBinding {
    const existingRoom = this.findByRoomId(input.matrixRoomId);
    if (existingRoom) {
      if (
        existingRoom.tenantId !== input.tenantId ||
        existingRoom.metaConnectionId !== input.metaConnectionId ||
        existingRoom.remoteThreadId !== input.remoteThreadId ||
        existingRoom.mautrixLoginId !== input.mautrixLoginId ||
        existingRoom.bridgeStateKey !== input.bridgeStateKey
      ) throw new Error("MATRIX_ROOM_ATTRIBUTION_CONFLICT");
      const now = new Date().toISOString();
      this.db.query(`UPDATE matrix_room_bindings
        SET source_event_id = ?, verified_at = ?, updated_at = ?
        WHERE matrix_room_id = ?`).run(input.sourceEventId, now, now, input.matrixRoomId);
      return this.findByRoomId(input.matrixRoomId)!;
    }

    const existingThread = this.findByRemoteThread(input.metaConnectionId, input.remoteThreadId);
    if (existingThread && existingThread.matrixRoomId !== input.matrixRoomId) {
      throw new Error("MATRIX_REMOTE_THREAD_ROOM_CONFLICT");
    }

    const connection = this.db.query(`SELECT tenant_id, mautrix_login_id FROM meta_connections WHERE id = ?`).get(input.metaConnectionId) as {
      tenant_id: string;
      mautrix_login_id: string | null;
    } | null;
    if (!connection) throw new Error("CONNECTION_NOT_FOUND");
    if (connection.tenant_id !== input.tenantId) throw new Error("CROSS_TENANT_MATRIX_ROOM_BINDING");
    if (connection.mautrix_login_id !== input.mautrixLoginId) throw new Error("MATRIX_LOGIN_IDENTITY_CONFLICT");

    const now = new Date().toISOString();
    this.db.query(`INSERT INTO matrix_room_bindings(
      matrix_room_id, tenant_id, meta_connection_id, remote_thread_id, mautrix_login_id,
      bridge_state_key, source_event_id, verified_at, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
      .run(
        input.matrixRoomId,
        input.tenantId,
        input.metaConnectionId,
        input.remoteThreadId,
        input.mautrixLoginId,
        input.bridgeStateKey,
        input.sourceEventId,
        now,
        now,
        now
      );
    return this.findByRoomId(input.matrixRoomId)!;
  }

  findByRoomId(matrixRoomId: string): MatrixRoomBinding | null {
    const row = this.db.query("SELECT * FROM matrix_room_bindings WHERE matrix_room_id = ?").get(matrixRoomId) as Record<string, unknown> | null;
    return row ? roomBindingFromRow(row) : null;
  }

  findByRemoteThread(metaConnectionId: string, remoteThreadId: string): MatrixRoomBinding | null {
    const row = this.db.query("SELECT * FROM matrix_room_bindings WHERE meta_connection_id = ? AND remote_thread_id = ?")
      .get(metaConnectionId, remoteThreadId) as Record<string, unknown> | null;
    return row ? roomBindingFromRow(row) : null;
  }

  list(): MatrixRoomBinding[] {
    return (this.db.query("SELECT * FROM matrix_room_bindings ORDER BY created_at, matrix_room_id").all() as Array<Record<string, unknown>>)
      .map(roomBindingFromRow);
  }
}

export class SQLiteMatrixSyncCheckpointRepository implements MatrixSyncCheckpointRepository {
  constructor(private readonly db: Database) {}

  get(consumerId: string): MatrixSyncCheckpoint | null {
    const row = this.db.query("SELECT * FROM matrix_sync_checkpoints WHERE consumer_id = ?").get(consumerId) as Record<string, unknown> | null;
    return row ? checkpointFromRow(row) : null;
  }

  save(consumerId: string, nextBatch: string): MatrixSyncCheckpoint {
    if (!consumerId || !nextBatch) throw new Error("INVALID_MATRIX_SYNC_CHECKPOINT");
    const now = new Date().toISOString();
    this.db.query(`INSERT INTO matrix_sync_checkpoints(consumer_id, next_batch, updated_at)
      VALUES (?, ?, ?)
      ON CONFLICT(consumer_id) DO UPDATE SET next_batch = excluded.next_batch, updated_at = excluded.updated_at`)
      .run(consumerId, nextBatch, now);
    return this.get(consumerId)!;
  }
}
