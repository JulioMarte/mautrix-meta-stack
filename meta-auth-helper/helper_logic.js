"use strict";

const PROTOCOL = "mautrix-meta-helper";
const RECAPTCHA_EXTRACT_JS = `new Promise((resolve, reject) => {
  window.FbLoginRecaptcha = {
    onRecaptcha: data => {
      try {
        resolve({recaptcha_token: JSON.parse(data)["g-recaptcha-response"]});
      } catch (err) {
        reject(err);
      }
    }
  }
})`;

function normalizeScript(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}


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
      host.endsWith(".messenger.com")
    );
  } catch (_) {
    return false;
  }
}

function allowedRecaptchaNavigation(raw) {
  try {
    const url = new URL(raw);
    const host = url.hostname.toLowerCase();
    return (
      url.protocol === "https:" &&
      (host === "fbsbx.com" || host.endsWith(".fbsbx.com")) &&
      url.pathname.startsWith("/captcha/recaptcha/iframe/")
    );
  } catch (_) {
    return false;
  }
}

function isMessengerLiteRecaptchaStep(step) {
  if (step?.type !== "cookies" || step?.step_id !== "fi.mau.meta.messengerlite.recaptcha") return false;
  const params = step.cookies || {};
  if (!allowedRecaptchaNavigation(String(params.url || ""))) return false;
  if (normalizeScript(params.extract_js) !== normalizeScript(RECAPTCHA_EXTRACT_JS)) return false;
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
  RECAPTCHA_EXTRACT_JS,
  findProtocolUrl,
  validatePairingUrl,
  cookieFields,
  allowedMetaNavigation,
  allowedRecaptchaNavigation,
  completionPattern,
  isMessengerLiteRecaptchaStep,
  sanitizeExtractedValues,
};
