import type { MatrixSyncIngestor } from "./matrix-sync-ingestor";

function positiveInt(raw: string | undefined, fallback: number): number {
  if (!raw) return fallback;
  const value = Number(raw);
  return Number.isSafeInteger(value) && value > 0 ? value : fallback;
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => { clearTimeout(timer); resolve(); }, { once: true });
  });
}

export class MatrixSyncRunner {
  private readonly initialBackoffMs: number;
  private readonly maxBackoffMs: number;

  constructor(
    private readonly ingestor: Pick<MatrixSyncIngestor, "runOnce">,
    env: Record<string, string | undefined> = process.env
  ) {
    this.initialBackoffMs = positiveInt(env.MATRIX_SYNC_INITIAL_BACKOFF_MS, 1_000);
    this.maxBackoffMs = positiveInt(env.MATRIX_SYNC_MAX_BACKOFF_MS, 30_000);
    if (this.maxBackoffMs < this.initialBackoffMs) throw new Error("MATRIX_SYNC_BACKOFF_CONFIGURATION_INVALID");
  }

  async run(signal: AbortSignal): Promise<void> {
    let backoffMs = this.initialBackoffMs;
    while (!signal.aborted) {
      try {
        const result = await this.ingestor.runOnce();
        backoffMs = this.initialBackoffMs;
        console.log(JSON.stringify({
          operation: "matrix_sync",
          result: "ok",
          status: result.status,
          roomsSeen: result.roomsSeen,
          roomsJoined: result.roomsJoined,
          roomsBound: result.roomsBound,
          eventsDelivered: result.eventsDelivered,
          eventsIgnored: result.eventsIgnored
        }));
      } catch (error) {
        if (signal.aborted) break;
        const code = error instanceof Error ? error.message : "MATRIX_SYNC_UNKNOWN_ERROR";
        console.error(JSON.stringify({ operation: "matrix_sync", result: "error", code, retryInMs: backoffMs }));
        await sleep(backoffMs, signal);
        backoffMs = Math.min(backoffMs * 2, this.maxBackoffMs);
      }
    }
  }
}
