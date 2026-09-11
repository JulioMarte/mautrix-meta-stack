import { describe, expect, test } from "bun:test";
import type { ChatwootBinding, SecretProvider } from "../src/domain/models";
import { HttpChatwootGateway } from "../src/services/http-chatwoot-gateway";

const binding: ChatwootBinding = {
  id: "binding-1",
  tenantId: "tenant-1",
  chatwootAccountId: "1",
  chatwootInboxId: "10",
  apiBaseUrl: "https://chatwoot.test",
  credentialRef: "env:CHATWOOT_TEST_TOKEN",
  status: "active",
  createdAt: "2026-09-11T00:00:00.000Z",
  updatedAt: "2026-09-11T00:00:00.000Z"
};

class StaticSecrets implements SecretProvider {
  resolve(ref: string): string | null { return ref === binding.credentialRef ? "chatwoot-canary-token" : null; }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

describe("Phase 4 Chatwoot HTTP gateway", () => {
  test("creates deterministic contact/source/conversation and authenticates every request", async () => {
    const requests: Array<{ url: URL; init: RequestInit | undefined }> = [];
    const fetchStub = async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
      const url = new URL(String(input));
      requests.push({ url, init });
      const path = url.pathname;
      if (path.endsWith("/contacts/filter")) return jsonResponse({ payload: [] });
      if (path.endsWith("/contacts") && init?.method === "POST") return jsonResponse({ payload: [{ id: 5, identifier: "contact-key", contact_inboxes: [{ source_id: "source-5", inbox: { id: 10 } }] }] });
      if (path.endsWith("/contacts/5/conversations")) return jsonResponse({ payload: [] });
      if (path.endsWith("/conversations") && init?.method === "POST") return jsonResponse({ id: 9, inbox_id: 10, custom_attributes: { mautrix_meta_remote_thread_id: "thread-1" } });
      throw new Error(`unexpected request ${init?.method ?? "GET"} ${path}`);
    };
    const gateway = new HttpChatwootGateway(new StaticSecrets(), fetchStub);
    const result = await gateway.ensureConversation({ binding, contactIdentifier: "contact-key", remoteContactId: "42", displayName: "Alice", remoteThreadId: "thread-1" });
    expect(result).toEqual({ contactId: "5", sourceId: "source-5", conversationId: "9" });
    expect(requests).toHaveLength(4);
    for (const request of requests) {
      expect(new Headers(request.init?.headers).get("api_access_token")).toBe("chatwoot-canary-token");
      expect(request.init?.redirect).toBe("error");
    }
    const createContactBody = JSON.parse(String(requests[1]!.init?.body));
    expect(createContactBody.identifier).toBe("contact-key");
    expect(createContactBody.inbox_id).toBe(10);
    const createConversationBody = JSON.parse(String(requests[3]!.init?.body));
    expect(createConversationBody.custom_attributes.mautrix_meta_remote_thread_id).toBe("thread-1");
  });

  test("ambiguous message create is reconciled by source event correlation before retry", async () => {
    let listCount = 0;
    let postCount = 0;
    const fetchStub = async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith("/messages") && (init?.method ?? "GET") === "GET") {
        listCount++;
        if (listCount === 1) return jsonResponse({ payload: [] });
        return jsonResponse({ payload: [{ id: 77, content_attributes: { mautrix_meta_source_event_id: "$evt" } }] });
      }
      if (path.endsWith("/messages") && init?.method === "POST") {
        postCount++;
        throw new TypeError("connection reset after request body was accepted");
      }
      throw new Error(`unexpected request ${init?.method ?? "GET"} ${path}`);
    };
    const gateway = new HttpChatwootGateway(new StaticSecrets(), fetchStub);
    const result = await gateway.createIncomingMessage({
      binding,
      conversation: { contactId: "5", sourceId: "source-5", conversationId: "9" },
      sourceEventId: "$evt",
      text: "hello",
      attachments: [],
      normalized: {
        tenantId: "tenant-1", connectionId: "connection-1", conversationExternalId: "thread-1",
        messageExternalId: "$evt", senderExternalId: "42", direction: "inbound", text: "hello",
        attachments: [], occurredAt: "2026-09-11T00:00:00.000Z", source: "matrix", sourceEventId: "$evt"
      }
    });
    expect(result.messageId).toBe("77");
    expect(postCount).toBe(1);
    expect(listCount).toBe(2);
  });

  test("missing Chatwoot credential fails before network access", async () => {
    let calls = 0;
    const noSecrets: SecretProvider = { resolve: () => null };
    const gateway = new HttpChatwootGateway(noSecrets, async () => { calls++; return jsonResponse({}); });
    expect(gateway.ensureConversation({ binding, contactIdentifier: "x", remoteContactId: "42", remoteThreadId: "t" })).rejects.toThrow("CHATWOOT_CREDENTIAL_MISSING");
    expect(calls).toBe(0);
  });
});
