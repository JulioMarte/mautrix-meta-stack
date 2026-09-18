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
    return (
      host === "facebook.com" ||
      host.endsWith(".facebook.com") ||
      host === "messenger.com" ||
      host.endsWith(".messenger.com") ||
      host === "fbsbx.com" ||
      host.endsWith(".fbsbx.com")
    );
  } catch (_) {
    return false;
  }
}

function isMessengerLiteRecaptchaStep(step) {
  if (step?.type !== "cookies" || step?.step_id !== "fi.mau.meta.messengerlite.recaptcha") return false;
  const params = step.cookies || {};
  if (typeof params.url !== "string" || !allowedMetaNavigation(params.url)) return false;
  if (typeof params.extract_js !== "string" || !params.extract_js.trim()) return false;
  const fields = cookieFields(step);
  return fields.some((field) => {
    if (field.id !== "recaptcha_token") return false;
    const sources = Array.isArray(field.sources) ? field.sources : [];
    return sources.some((source) => source?.type === "special" && source?.name === "recaptcha_token");
  });
}

function sanitizeExtractedValues(step, raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("The authentication challenge returned an invalid result");
  }
  const allowed = new Set(cookieFields(step).map((field) => field.id));
  const values = {};
  for (const [key, value] of Object.entries(raw)) {
    if (allowed.has(key) && typeof value === "string" && value && value.length <= 8192) {
      values[key] = value;
    }
  }
  const missing = cookieFields(step)
    .filter((field) => field.required !== false && !values[field.id])
    .map((field) => field.id);
  if (missing.length) {
    throw new Error("The authentication challenge did not return all required fields");
  }
  return values;
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
  isMessengerLiteRecaptchaStep,
  sanitizeExtractedValues,
};
