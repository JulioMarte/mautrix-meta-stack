import type { MatrixSyncReadiness } from "./matrix-sync-runner";

export function matrixSyncReadinessGuard(
  readiness: MatrixSyncReadiness,
  request: Request,
  set: { status?: number | string }
): { error: { code: string; message: string } } | undefined {
  let path: string;
  try { path = new URL(request.url).pathname; }
  catch { return undefined; }
  if (path !== "/health/ready") return undefined;
  if (readiness.snapshot().ready) return undefined;
  set.status = 503;
  return { error: { code: "MATRIX_SYNC_NOT_READY", message: "Matrix ingestion is not ready" } };
}
