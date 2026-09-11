import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { createApp } from "./app";
import { createPhase5App } from "./phase5-app";
import { createProvisioningApp } from "./provisioning-app";
import { openDatabase } from "./persistence/database";
import { LATEST_SCHEMA_VERSION } from "./persistence/migrations";
import { ChatwootEnvironmentSecretProvider } from "./security/secrets";
import { HttpChatwootGateway } from "./services/http-chatwoot-gateway";
import { HttpMatrixMediaDownloader } from "./services/matrix-media-downloader";

const databasePath = process.env.CONTROL_PLANE_DB_PATH ?? "/data/control-plane.db";
const adminToken = process.env.CONTROL_PLANE_ADMIN_TOKEN ?? "";
const internalToken = process.env.CONTROL_PLANE_INTERNAL_TOKEN ?? "";
const port = Number(process.env.PORT ?? "3000");

if (adminToken.length < 16) throw new Error("CONTROL_PLANE_ADMIN_TOKEN must be set and at least 16 characters");
if (internalToken.length < 24) throw new Error("CONTROL_PLANE_INTERNAL_TOKEN must be set and at least 24 characters");
if (databasePath !== ":memory:") mkdirSync(dirname(databasePath), { recursive: true });

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
console.log(JSON.stringify({ operation: "startup", result: "ok", port, schema: LATEST_SCHEMA_VERSION }));

process.on("SIGTERM", () => { app.stop(); db.close(); process.exit(0); });
