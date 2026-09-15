import { createHmac, timingSafeEqual } from "node:crypto";

export type ChatwootWebhookHeaders = {
  timestamp: string | null;
  signature: string | null;
  deliveryId: string | null;
};

function safeEqualHex(left: string, right: string): boolean {
  if (!/^[0-9a-f]{64}$/i.test(left) || !/^[0-9a-f]{64}$/i.test(right)) return false;
  const a = Buffer.from(left, "hex");
  const b = Buffer.from(right, "hex");
  return a.length === b.length && timingSafeEqual(a, b);
}

export function chatwootWebhookHeaders(request: Request): ChatwootWebhookHeaders {
  return {
    timestamp: request.headers.get("x-chatwoot-timestamp"),
    signature: request.headers.get("x-chatwoot-signature"),
    deliveryId: request.headers.get("x-chatwoot-delivery")
  };
}

export function verifyChatwootWebhook(input: {
  rawBody: string;
  secret: string;
  timestamp: string | null;
  signature: string | null;
  nowMs?: number;
  toleranceSeconds?: number;
}): boolean {
  if (!input.secret || !input.timestamp || !input.signature?.startsWith("sha256=")) return false;
  if (!/^[0-9]{10,13}$/.test(input.timestamp)) return false;
  const rawTimestamp = Number(input.timestamp);
  if (!Number.isSafeInteger(rawTimestamp)) return false;
  const timestampMs = input.timestamp.length === 13 ? rawTimestamp : rawTimestamp * 1000;
  const nowMs = input.nowMs ?? Date.now();
  const toleranceMs = (input.toleranceSeconds ?? 300) * 1000;
  if (Math.abs(nowMs - timestampMs) > toleranceMs) return false;
  const expected = createHmac("sha256", input.secret).update(`${input.timestamp}.${input.rawBody}`).digest("hex");
  return safeEqualHex(input.signature.slice(7), expected);
}
