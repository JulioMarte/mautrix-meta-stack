import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { createApp } from "./app";
import { createPhase5App } from "./phase5-app";
import { createProvisioningApp } from "./provisioning-app";
import { openDatabase } from "./persistence/database";
import { LATEST_SCHEMA_VERSION } from "./persistence/migrations";
import { SQLiteMatrixRoomBindingRepository, SQLiteMatrixSyncCheckpointRepository } from "./persistence/sqlite-matrix-repositories";
import {
  SQLiteChatwootBindingRepository,
  SQLiteConversationBindingRepository,
  SQLiteMetaConnectionRepository,
  SQLiteProcessedEventRepository,
  SQLiteTenantRepository
} from "./persistence/sqlite-repositories";
import { ChatwootEnvironmentSecretProvider } from "./security/secrets";
import { HttpChatwootGateway } from "./services/http-chatwoot-gateway";
import { MatrixRoomAttributionService } from "./services/matrix-room-attribution";
import { HttpMatrixMediaDownloader } from "./services/matrix-media-downloader";
import { HttpMatrixSyncClient } from "./services/matrix-sync-client";
import { MatrixSyncIngestor } from "./services/matrix-sync-ingestor";
import { MatrixSyncRunner } from "./services/matrix-sync-runner";
import { MatrixToChatwootService } from "./services/matrix-to-chatwoot";

const databasePath = process.env.CONTROL_PLANE_DB_PATH ?? "/data/control-plane.db";
const adminToken = process.env.CONTROL_PLANE_ADMIN_TOKEN ?? "";
const internalToken = process.env.CONTROL_PLANE_INTERNAL_TOKEN ?? "";
const port = Number(process.env.PORT ?? "3000");

if (adminToken.length < 16) throw new Error("CONTROL_PLANE_ADMIN_TOKEN must be set and at least 16 characters");
if (internalToken.length < 24) throw new Error("CONTROL_PLANE_INTERNAL_TOKEN must be set and at least 24 characters");
if (databasePath !== ":memory:") mkdirSync(dirname(databasePath), { recursive: true });

const syncEnabledRaw = process.env.MATRIX_SYNC_ENABLED ?? "false";
if (syncEnabledRaw !== "true" && syncEnabledRaw !== "false") throw new Error("MATRIX_SYNC_ENABLED must be true or false");
const syncEnabled = syncEnabledRaw === "true";

const db = openDatabase(databasePath);
const chatwootGateway = new HttpChatwootGateway(
  new ChatwootEnvironmentSecretProvider(process.env),
  fetch,
  8_000,
  new HttpMatrixMediaDownloader(process.env)
);
const app = createApp(db, adminToken, internalToken, process.env, { chatwootGateway })
  .use(createPhase5App(db, adminToken, process.env))
  .use(createProvisioningApp(db, adminToken, internalToken, process.env))
  .listen({ hostname: "0.0.0.0", port });

const shutdownController = new AbortController();
let matrixSyncTask: Promise<void> | null = null;
if (syncEnabled) {
  const tenants = new SQLiteTenantRepository(db);
  const connections = new SQLiteMetaConnectionRepository(db);
  const chatwootBindings = new SQLiteChatwootBindingRepository(db);
  const conversationBindings = new SQLiteConversationBindingRepository(db);
  const processedEvents = new SQLiteProcessedEventRepository(db);
  const roomBindings = new SQLiteMatrixRoomBindingRepository(db);
  const checkpoints = new SQLiteMatrixSyncCheckpointRepository(db);
  const matrixToChatwoot = new MatrixToChatwootService(
    tenants,
    connections,
    chatwootBindings,
    conversationBindings,
    processedEvents,
    chatwootGateway
  );
  const syncClient = new HttpMatrixSyncClient(process.env);
  const attribution = new MatrixRoomAttributionService(connections, roomBindings, process.env);
  const ingestor = new MatrixSyncIngestor(
    process.env.MATRIX_SYNC_CONSUMER_ID ?? "matrix-synapse-ingestor-v1",
    syncClient,
    attribution,
    roomBindings,
    checkpoints,
    matrixToChatwoot,
    process.env
  );
  const runner = new MatrixSyncRunner(ingestor, process.env);
  matrixSyncTask = runner.run(shutdownController.signal).catch((error) => {
    const code = error instanceof Error ? error.message : "MATRIX_SYNC_RUNNER_FAILED";
    console.error(JSON.stringify({ operation: "matrix_sync_runner", result: "fatal", code }));
  });
}

console.log(JSON.stringify({ operation: "startup", result: "ok", port, schema: LATEST_SCHEMA_VERSION, matrixSyncEnabled: syncEnabled }));

let shuttingDown = false;
async function shutdown(): Promise<void> {
  if (shuttingDown) return;
  shuttingDown = true;
  shutdownController.abort();
  app.stop();
  await matrixSyncTask?.catch(() => undefined);
  db.close();
  process.exit(0);
}

process.on("SIGTERM", () => { void shutdown(); });
process.on("SIGINT", () => { void shutdown(); });
