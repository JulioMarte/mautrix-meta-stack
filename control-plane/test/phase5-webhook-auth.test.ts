import { describe, expect, test } from "bun:test";
import { createHmac } from "node:crypto";
import { verifyChatwootWebhook } from "../src/services/chatwoot-webhook-auth";

function signature(secret: string, timestamp: string, body: string): string {
  return `sha256=${createHmac("sha256", secret).update(`${timestamp}.${body}`).digest("hex")}`;
}

describe("Phase 5 Chatwoot webhook authentication", () => {
  test("accepts the official timestamp.raw-body HMAC contract", () => {
    const body = JSON.stringify({ event: "message_created", id: 42 });
    const timestamp = "1789128000";
    const secret = "phase5-webhook-secret";
    expect(verifyChatwootWebhook({ rawBody: body, secret, timestamp, signature: signature(secret, timestamp, body), nowMs: 1789128000_000 })).toBe(true);
  });

  test("rejects tampered, stale, malformed and missing signatures", () => {
    const body = "{\"id\":42}";
    const timestamp = "1789128000";
    const secret = "phase5-webhook-secret";
    const signed = signature(secret, timestamp, body);
    expect(verifyChatwootWebhook({ rawBody: `${body} `, secret, timestamp, signature: signed, nowMs: 1789128000_000 })).toBe(false);
    expect(verifyChatwootWebhook({ rawBody: body, secret, timestamp, signature: signed, nowMs: 1789129000_000 })).toBe(false);
    expect(verifyChatwootWebhook({ rawBody: body, secret, timestamp: "nope", signature: signed, nowMs: 1789128000_000 })).toBe(false);
    expect(verifyChatwootWebhook({ rawBody: body, secret, timestamp, signature: null, nowMs: 1789128000_000 })).toBe(false);
  });
});
