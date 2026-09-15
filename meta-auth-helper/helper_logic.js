"use strict";

const PROTOCOL = "mautrix-meta-helper";

function findProtocolUrl(argv) {
  return argv.find((arg) => typeof arg === "string" && arg.startsWith(`${PROTOCOL}://`)) || "";
}

function validatePairingUrl(raw) {
  const parsed = new URL(raw);
  if (parsed.protocol !== `${PROTOCOL}:` || parsed.hostname !== "connect") {
    throw new Error("Invalid helper link");
  }
  const originText = parsed.searchParams.get("origin") || "";
  const id = parsed.searchParams.get("id") || "";
  const token = parsed.searchParams.get("token") || "";
  if (!originText || !id || !token || id.length > 128 || token.length > 256) {
    throw new Error("Incomplete helper pairing link");
  }
  const origin = new URL(originText);
  const localhost = ["localhost", "127.0.0.1", "::1"].includes(origin.hostname);
  if (origin.protocol !== "https:" && !(localhost && origin.protocol === "http:")) {
    throw new Error("The integration server must use HTTPS");
  }
  if (origin.username || origin.password || origin.pathname !== "/" || origin.search || origin.hash) {
    throw new Error("Invalid integration server origin");
  }
  return { origin: origin.origin, id, token };
}

function cookieFields(step) {
  const fields = step?.cookies?.fields;
  return Array.isArray(fields) ? fields.filter((field) => field && typeof field.id === "string") : [];
}

function allowedMetaNavigation(raw) {
  try {
    const url = new URL(raw);
    if (url.protocol !== "https:") return false;
    const host = url.hostname.toLowerCase();
    return host === "facebook.com" || host.endsWith(".facebook.com") || host === "messenger.com" || host.endsWith(".messenger.com");
  } catch (_) {
    return false;
  }
}

function completionPattern(raw) {
  try {
    return new RegExp(String(raw || "^https://"));
  } catch (_) {
    throw new Error("The bridge returned an invalid completion URL pattern");
  }
}

module.exports = {
  PROTOCOL,
  findProtocolUrl,
  validatePairingUrl,
  cookieFields,
  allowedMetaNavigation,
  completionPattern,
};
