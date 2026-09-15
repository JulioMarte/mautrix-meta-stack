import { afterEach, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { createApp } from "../src/app";
import { runMigrations } from "../src/persistence/migrations";
import { matrixSyncReadinessGuard } from "../src/services/matrix-sync-readiness";
import { MatrixSyncReadiness } from "../src/services/matrix-sync-runner";

let db: Database | undefined;
afterEach(() => { db?.close(); db = undefined; });

function appWithReadiness(readiness: MatrixSyncReadiness) {
  db = new Database(":memory:", { strict: true });
  runMigrations(db);
  return createApp(db, "test-admin-token-123456", "test-internal-token-123456789")
    .onRequest(({ request, set }) => matrixSyncReadinessGuard(readiness, request, set));
}

describe("Matrix sync readiness", () => {
  test("health is not ready until the first successful sync", async () => {
    const readiness = new MatrixSyncReadiness();
    const app = appWithReadiness(readiness);
    const live = await app.handle(new Request("http://localhost/health/live"));
    expect(live.status).toBe(200);
    const ready = await app.handle(new Request("http://localhost/health/ready"));
    expect(ready.status).toBe(503);
    expect(await ready.json()).toEqual({
      error: { code: "MATRIX_SYNC_NOT_READY", message: "Matrix ingestion is not ready" }
    });
  });

  test("successful sync enables readiness and a later sync failure removes it", async () => {
    const readiness = new MatrixSyncReadiness();
    const app = appWithReadiness(readiness);

    readiness.markSuccess();
    const healthy = await app.handle(new Request("http://localhost/health/ready"));
    expect(healthy.status).toBe(200);
    expect(await healthy.json()).toMatchObject({ status: "ready" });
    expect(readiness.snapshot().lastSuccessAt).not.toBeNull();

    readiness.markFailure("MATRIX_SYNC_UNAUTHORIZED");
    const unhealthy = await app.handle(new Request("http://localhost/health/ready"));
    expect(unhealthy.status).toBe(503);
    expect(await unhealthy.json()).toEqual({
      error: { code: "MATRIX_SYNC_NOT_READY", message: "Matrix ingestion is not ready" }
    });
    expect(readiness.snapshot().code).toBe("MATRIX_SYNC_UNAUTHORIZED");
    expect(readiness.snapshot().lastFailureAt).not.toBeNull();
  });

  test("guard only affects the readiness endpoint", async () => {
    const readiness = new MatrixSyncReadiness();
    const app = appWithReadiness(readiness);
    const denied = await app.handle(new Request("http://localhost/api/v1/tenants"));
    expect(denied.status).toBe(401);
  });
});
