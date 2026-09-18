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
    return [
      "facebook.com",
      "messenger.com",
      "fbsbx.com",
      "google.com",
      "recaptcha.net",
    ].some((domain) => host === domain || host.endsWith(`.${domain}`));
  } catch (_) {
    return false;
  }
}

function fieldSources(field) {
  return Array.isArray(field?.sources)
    ? field.sources.filter((source) => source && typeof source.type === "string" && typeof source.name === "string")
    : [];
}

function cookieSourceNames(field) {
  const names = fieldSources(field)
    .filter((source) => source.type === "cookie")
    .map((source) => source.name)
    .filter(Boolean);
  return names.length ? names : [field.id];
}

function isRecaptchaField(field) {
  return field?.id === "recaptcha_token" && fieldSources(field).some(
    (source) => source.type === "special" && source.name === "recaptcha_token"
  );
}

function interactiveExtractScript(step) {
  const params = step?.cookies || {};
  const fields = cookieFields(step);
  if (!fields.some(isRecaptchaField)) return "";
  return typeof params.extract_js === "string" ? params.extract_js : "";
}

function normalizeExtractedValues(step, result) {
  if (!result || typeof result !== "object" || Array.isArray(result)) {
    throw new Error("The interactive challenge did not return a field map");
  }
  const fields = cookieFields(step);
  const allowed = new Set(fields.map((field) => field.id));
  const values = {};
  for (const [key, value] of Object.entries(result)) {
    if (!allowed.has(key) || typeof value !== "string" || !value) continue;
    values[key] = value;
  }
  const missing = fields
    .filter((field) => field.required !== false && !values[field.id])
    .map((field) => field.id);
  return { values, missing };
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
  fieldSources,
  cookieSourceNames,
  isRecaptchaField,
  interactiveExtractScript,
  normalizeExtractedValues,
  completionPattern,
};
