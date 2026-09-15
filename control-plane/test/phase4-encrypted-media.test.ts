import { describe, expect, test } from "bun:test";
import type { MatrixEncryptedFile } from "../src/domain/models";
import { HttpMatrixMediaDownloader } from "../src/services/matrix-media-downloader";

function base64(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString("base64").replace(/=+$/, "");
}
function base64Url(bytes: Uint8Array): string {
  return base64(bytes).replace(/\+/g, "-").replace(/\//g, "_");
}
function arrayBuffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
}

async function encryptedFixture(plaintext: string): Promise<{ ciphertext: Uint8Array; metadata: MatrixEncryptedFile }> {
  const keyBytes = Uint8Array.from({ length: 32 }, (_, index) => index + 1);
  const iv = new Uint8Array(16);
  iv.set([11, 22, 33, 44, 55, 66, 77, 88], 0);
  const key = await crypto.subtle.importKey("raw", arrayBuffer(keyBytes), { name: "AES-CTR" }, false, ["encrypt"]);
  const encoded = new TextEncoder().encode(plaintext);
  const cipherBuffer = await crypto.subtle.encrypt({ name: "AES-CTR", counter: iv, length: 64 }, key, arrayBuffer(encoded));
  const ciphertext = new Uint8Array(cipherBuffer);
  const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", cipherBuffer));
  return {
    ciphertext,
    metadata: {
      v: "v2",
      key: { kty: "oct", alg: "A256CTR", k: base64Url(keyBytes), keyOps: ["encrypt", "decrypt"], ext: true },
      iv: base64(iv),
      hashes: { sha256: base64(hash) }
    }
  };
}

describe("Phase 4 encrypted Matrix attachments", () => {
  test("verifies ciphertext hash and decrypts AES-256-CTR before Chatwoot upload", async () => {
    const fixture = await encryptedFixture("secret voice note");
    const downloader = new HttpMatrixMediaDownloader({ MATRIX_MEDIA_ACCESS_TOKEN: "media-token", MATRIX_MEDIA_MAX_BYTES: "4096" }, async () =>
      new Response(arrayBuffer(fixture.ciphertext), { status: 200, headers: { "content-type": "application/octet-stream" } })
    );
    const result = await downloader.download({
      kind: "audio",
      url: "mxc://matrix.example.com/encrypted-voice",
      mimeType: "audio/ogg",
      fileName: "voice.ogg",
      encryption: fixture.metadata
    });
    expect(result.mimeType).toBe("audio/ogg");
    expect(result.fileName).toBe("voice.ogg");
    expect(await result.blob.text()).toBe("secret voice note");
  });

  test("tampered encrypted media fails closed before decryption", async () => {
    const fixture = await encryptedFixture("original photo");
    fixture.ciphertext[0] = fixture.ciphertext[0]! ^ 0xff;
    const downloader = new HttpMatrixMediaDownloader({ MATRIX_MEDIA_ACCESS_TOKEN: "media-token" }, async () =>
      new Response(arrayBuffer(fixture.ciphertext), { status: 200 })
    );
    await expect(downloader.download({
      kind: "image",
      url: "mxc://matrix.example.com/tampered",
      mimeType: "image/jpeg",
      encryption: fixture.metadata
    })).rejects.toThrow("MATRIX_MEDIA_HASH_MISMATCH");
  });
});
