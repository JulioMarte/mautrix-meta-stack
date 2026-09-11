import { afterEach, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import type { ChatwootGateway } from "../src/services/chatwoot-gateway";
import { runMigrations } from "../src/persistence/migrations";
import {
  SQLiteMatrixRoomBindingRepository,
  SQLiteMatrixSyncCheckpointRepository
} from "../src/persistence/sqlite-matrix-repositories";
import {
  SQLiteChatwootBindingRepository,
  SQLiteConversationBindingRepository,
  SQLiteMetaConnectionRepository,
  SQLiteProcessedEventRepository,
  SQLiteTenantRepository
} from "../src/persistence/sqlite-repositories";
import { MatrixRoomAttributionService } from "../src/services/matrix-room-attribution";
import { MatrixSyncIngestor } from "../src/services/matrix-sync-ingestor";
import type { MatrixSyncResponse } from "../src/services/matrix-sync-client";
import { MatrixToChatwootService } from "../src/services/matrix-to-chatwoot";

let db: Database | undefined;
afterEach(() => { db?.close(); db = undefined; });

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

function metaText(eventId: string, body: string) {
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

describe("Matrix sync and Phase 4 retry composition", () => {
  test("does not advance checkpoint on a partial retryable failure and deduplicates earlier side effects on replay", async () => {
    db = new Database(":memory:", { strict: true });
    runMigrations(db);

    const tenants = new SQLiteTenantRepository(db);
    const connections = new SQLiteMetaConnectionRepository(db);
    const chatwootBindings = new SQLiteChatwootBindingRepository(db);
    const conversationBindings = new SQLiteConversationBindingRepository(db);
    const processedEvents = new SQLiteProcessedEventRepository(db);
    const roomBindings = new SQLiteMatrixRoomBindingRepository(db);
    const checkpoints = new SQLiteMatrixSyncCheckpointRepository(db);

    const tenant = tenants.create({ slug: "retry-a", name: "Retry A" });
    const connection = connections.create({ tenantId: tenant.id, matrixOwnerMxid: "@owner:matrix.example.com" });
    connections.setProviderIdentity(connection.id, { metaAccountId: "1001", mautrixLoginId: "login-a" });
    const chatwoot = chatwootBindings.create({
      tenantId: tenant.id,
      chatwootAccountId: "1",
      chatwootInboxId: "2",
      apiBaseUrl: "http://chatwoot.example.com",
      credentialRef: "env:CHATWOOT_RETRY_TOKEN",
      status: "active"
    });
    connections.assignChatwootBinding(connection.id, chatwoot.id);
    connections.setStatus(connection.id, "active");

    const attempts = new Map<string, number>();
    const successfulSideEffects: string[] = [];
    const gateway: ChatwootGateway = {
      async ensureConversation() {
        return { contactId: "contact-1", sourceId: "source-1", conversationId: "conversation-1" };
      },
      async createIncomingMessage(input) {
        const count = (attempts.get(input.sourceEventId) ?? 0) + 1;
        attempts.set(input.sourceEventId, count);
        if (input.sourceEventId === "$second" && count === 1) throw new Error("CHATWOOT_REQUEST_FAILED");
        successfulSideEffects.push(input.sourceEventId);
        return { messageId: `message-${input.sourceEventId}` };
      }
    };

    const matrixToChatwoot = new MatrixToChatwootService(
      tenants,
      connections,
      chatwootBindings,
      conversationBindings,
      processedEvents,
      gateway
    );
    const attribution = new MatrixRoomAttributionService(connections, roomBindings, {
      MATRIX_BRIDGE_BOT_MXID: "@metabot:matrix.example.com",
      MATRIX_BRIDGE_PROTOCOL_IDS: "facebookgo"
    });

    checkpoints.save("matrix-ingestor", "s1");
    const batch: MatrixSyncResponse = {
      next_batch: "s2",
      rooms: { join: {
        "!portal:matrix.example.com": {
          state: { events: bridgeState() },
          timeline: { events: [metaText("$first", "first"), metaText("$second", "second")] }
        }
      }, invite: {} }
    };
    const seenSince: Array<string | null> = [];
    const transport = {
      async sync(since: string | null) { seenSince.push(since); return batch; },
      async roomState() { return bridgeState(); },
      async joinRoom() {}
    };
    const ingestor = new MatrixSyncIngestor(
      "matrix-ingestor",
      transport,
      attribution,
      roomBindings,
      checkpoints,
      matrixToChatwoot,
      { MATRIX_SYNC_USER_MXID: "@ingestor:matrix.example.com" }
    );

    await expect(ingestor.runOnce()).rejects.toThrow("CHATWOOT_REQUEST_FAILED");
    expect(checkpoints.get("matrix-ingestor")?.nextBatch).toBe("s1");
    expect(successfulSideEffects).toEqual(["$first"]);
    expect(attempts.get("$first")).toBe(1);
    expect(attempts.get("$second")).toBe(1);

    const recovered = await ingestor.runOnce();
    expect(recovered.nextCheckpoint).toBe("s2");
    expect(checkpoints.get("matrix-ingestor")?.nextBatch).toBe("s2");
    expect(seenSince).toEqual(["s1", "s1"]);
    expect(attempts.get("$first")).toBe(1);
    expect(attempts.get("$second")).toBe(2);
    expect(successfulSideEffects).toEqual(["$first", "$second"]);
    expect(processedEvents.find("matrix", "$first")?.status).toBe("delivered");
    expect(processedEvents.find("matrix", "$second")?.status).toBe("delivered");
  });
});
