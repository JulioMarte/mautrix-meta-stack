import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { createApp } from "./app";
import { openDatabase } from "./persistence/database";

const databasePath = process.env.CONTROL_PLANE_DB_PATH ?? "/data/control-plane.db";
const adminToken = process.env.CONTROL_PLANE_ADMIN_TOKEN ?? "";
const port = Number(process.env.PORT ?? "3000");

if (adminToken.length < 16) {
  throw new Error("CONTROL_PLANE_ADMIN_TOKEN must be set and at least 16 characters");
}
if (databasePath !== ":memory:") mkdirSync(dirname(databasePath), { recursive: true });

const db = openDatabase(databasePath);
const app = createApp(db, adminToken).listen({ hostname: "0.0.0.0", port });
console.log(JSON.stringify({ operation: "startup", result: "ok", port, schema: 1 }));

process.on("SIGTERM", () => {
  app.stop();
  db.close();
  process.exit(0);
});
