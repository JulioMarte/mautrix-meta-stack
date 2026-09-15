import { describe, expect, test } from "bun:test";
import {
  ChatwootEnvironmentSecretProvider, ChatwootWebhookEnvironmentSecretProvider,
  isSupportedChatwootSecretRef, isSupportedChatwootWebhookSecretRef
} from "../src/security/secrets";

describe("Phase 5 Chatwoot secret boundaries", () => {
  test("API credentials cannot be used as webhook signing secrets and vice versa", () => {
    expect(isSupportedChatwootSecretRef("env:CHATWOOT_TENANT_A_TOKEN")).toBe(true);
    expect(isSupportedChatwootSecretRef("env:CHATWOOT_WEBHOOK_TENANT_A_SECRET")).toBe(false);
    expect(isSupportedChatwootWebhookSecretRef("env:CHATWOOT_WEBHOOK_TENANT_A_SECRET")).toBe(true);
    expect(isSupportedChatwootWebhookSecretRef("env:CHATWOOT_TENANT_A_TOKEN")).toBe(false);
  });

  test("providers resolve only their dedicated environment namespace", () => {
    const env = {
      CHATWOOT_TENANT_A_TOKEN: "api-token",
      CHATWOOT_WEBHOOK_TENANT_A_SECRET: "webhook-secret"
    };
    const api = new ChatwootEnvironmentSecretProvider(env);
    const webhook = new ChatwootWebhookEnvironmentSecretProvider(env);
    expect(api.resolve("env:CHATWOOT_TENANT_A_TOKEN")).toBe("api-token");
    expect(api.resolve("env:CHATWOOT_WEBHOOK_TENANT_A_SECRET")).toBeNull();
    expect(webhook.resolve("env:CHATWOOT_WEBHOOK_TENANT_A_SECRET")).toBe("webhook-secret");
    expect(webhook.resolve("env:CHATWOOT_TENANT_A_TOKEN")).toBeNull();
  });
});
