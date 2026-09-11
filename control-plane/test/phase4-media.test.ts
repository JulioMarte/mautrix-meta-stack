import { describe, expect, test } from "bun:test";
import type { ChatwootBinding, SecretProvider } from "../src/domain/models";
import { HttpChatwootGateway } from "../src/services/http-chatwoot-gateway";
import { HttpMatrixMediaDownloader, type MatrixMediaDownloader } from "../src/services/matrix-media-downloader";

const binding: ChatwootBinding = {
  id: "binding-media", tenantId: "tenant-1", chatwootAccountId: "1", chatwootInboxId: "10",
  apiBaseUrl: "https://chatwoot.test", credentialRef: "env:CHATWOOT_TEST_TOKEN", status: "active",
  createdAt: "2026-09-11T00:00:00.000Z", updatedAt: "2026-09-11T00:00:00.000Z"
};

const secrets: SecretProvider = { resolve: () => "chatwoot-canary-token" };
const conversation = { contactId: "5", sourceId: "source-5", conversationId: "9" };

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function normalized(sourceEventId: string) {
  return {
    tenantId: "tenant-1", connectionId: "connection-1", conversationExternalId: "thread-1",
    messageExternalId: sourceEventId, senderExternalId: "42", direction: "inbound" as const,
    attachments: [], occurredAt: "2026-09-11T00:00:00.000Z", source: "matrix" as const, sourceEventId
  };
}

describe("Phase 4 Matrix media downloader", () => {
  test("downloads authenticated mxc media through the v1 client media endpoint", async () => {
    const calls: Array<{ url: URL; authorization: string | null; redirect: RequestRedirect | undefined }> = [];
    const fetchStub = async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
      calls.push({ url: new URL(String(input)), authorization: new Headers(init?.headers).get("authorization"), redirect: init?.redirect });
      return new Response("voice-bytes", { status: 200, headers: { "content-type": "audio/ogg", "content-length": "11" } });
    };
    const downloader = new HttpMatrixMediaDownloader({
      MATRIX_MEDIA_BASE_URL: "http://synapse:8008",
      MATRIX_MEDIA_ACCESS_TOKEN: "matrix-media-canary",
      MATRIX_MEDIA_MAX_BYTES: "1024"
    }, fetchStub);
    const result = await downloader.download({ kind: "audio", url: "mxc://matrix.example.com/media123", mimeType: "audio/ogg", fileName: "voice.ogg", sizeBytes: 11 });
    expect(calls).toHaveLength(1);
    expect(calls[0]!.url.pathname).toBe("/_matrix/client/v1/media/download/matrix.example.com/media123");
    expect(calls[0]!.authorization).toBe("Bearer matrix-media-canary");
    expect(calls[0]!.redirect).toBe("manual");
    expect(result.fileName).toBe("voice.ogg");
    expect(result.mimeType).toBe("audio/ogg");
    expect(await result.blob.text()).toBe("voice-bytes");
  });

  test("rejects arbitrary http attachment URLs and oversized media", async () => {
    let calls = 0;
    const downloader = new HttpMatrixMediaDownloader({ MATRIX_MEDIA_ACCESS_TOKEN: "token", MATRIX_MEDIA_MAX_BYTES: "4" }, async () => {
      calls++;
      return new Response("12345", { headers: { "content-length": "5" } });
    });
    await expect(downloader.download({ kind: "file", url: "https://evil.test/file.pdf" })).rejects.toThrow("MATRIX_MEDIA_URI_INVALID");
    expect(calls).toBe(0);
    await expect(downloader.download({ kind: "file", url: "mxc://example.test/id", fileName: "x.pdf" })).rejects.toThrow("MATRIX_MEDIA_TOO_LARGE");
  });
});

describe("Phase 4 Chatwoot multipart attachments", () => {
  test("uploads image, voice note and PDF as attachments[] while preserving correlation", async () => {
    const media: MatrixMediaDownloader = {
      async download(attachment) {
        const content = `${attachment.kind}-bytes`;
        return {
          blob: new Blob([content], { type: attachment.mimeType ?? "application/octet-stream" }),
          fileName: attachment.fileName ?? `${attachment.kind}.bin`,
          mimeType: attachment.mimeType ?? "application/octet-stream",
          sizeBytes: content.length
        };
      }
    };
    let postedForm: FormData | null = null;
    let listCount = 0;
    const fetchStub = async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith("/messages") && (init?.method ?? "GET") === "GET") {
        listCount++;
        return jsonResponse({ payload: [] });
      }
      if (path.endsWith("/messages") && init?.method === "POST") {
        expect(init.body).toBeInstanceOf(FormData);
        postedForm = init.body as FormData;
        expect(new Headers(init.headers).has("content-type")).toBe(false);
        return jsonResponse({ id: 501 });
      }
      throw new Error(`unexpected request ${init?.method ?? "GET"} ${path}`);
    };
    const gateway = new HttpChatwootGateway(secrets, fetchStub, 8_000, media);
    const attachments = [
      { kind: "image" as const, url: "mxc://example/image", mimeType: "image/jpeg", fileName: "photo.jpg" },
      { kind: "audio" as const, url: "mxc://example/audio", mimeType: "audio/ogg", fileName: "voice.ogg" },
      { kind: "file" as const, url: "mxc://example/pdf", mimeType: "application/pdf", fileName: "invoice.pdf" }
    ];
    const result = await gateway.createIncomingMessage({ binding, conversation, sourceEventId: "$media-event", text: "media", attachments, normalized: { ...normalized("$media-event"), text: "media", attachments } });
    expect(result.messageId).toBe("501");
    expect(listCount).toBe(1);
    const form = postedForm!;
    expect(form.get("message_type")).toBe("incoming");
    expect(form.get("private")).toBe("false");
    expect(form.get("content_attributes[mautrix_meta_source_event_id]")).toBe("$media-event");
    const files = form.getAll("attachments[]") as File[];
    expect(files).toHaveLength(3);
    expect(files.map((file) => [file.name, file.type])).toEqual([
      ["photo.jpg", "image/jpeg"], ["voice.ogg", "audio/ogg"], ["invoice.pdf", "application/pdf"]
    ]);
  });

  test("refuses more than Chatwoot's 15 attachment model limit before posting", async () => {
    const media: MatrixMediaDownloader = { async download() { return { blob: new Blob(["x"]), fileName: "x", mimeType: "application/octet-stream", sizeBytes: 1 }; } };
    let posts = 0;
    const fetchStub = async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith("/messages") && (init?.method ?? "GET") === "GET") return jsonResponse({ payload: [] });
      if (init?.method === "POST") posts++;
      return jsonResponse({ id: 1 });
    };
    const gateway = new HttpChatwootGateway(secrets, fetchStub, 8_000, media);
    const attachments = Array.from({ length: 16 }, (_, index) => ({ kind: "file" as const, url: `mxc://example/${index}` }));
    await expect(gateway.createIncomingMessage({ binding, conversation, sourceEventId: "$too-many", attachments, normalized: { ...normalized("$too-many"), attachments } })).rejects.toThrow("CHATWOOT_ATTACHMENT_LIMIT_EXCEEDED");
    expect(posts).toBe(0);
  });
});
