"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  findProtocolUrl,
  validatePairingUrl,
  cookieFields,
  allowedMetaNavigation,
  completionPattern,
  isMessengerLiteRecaptchaStep,
  sanitizeExtractedValues,
} = require("../helper_logic");

test("deep link accepts HTTPS integration origin", () => {
  const parsed = validatePairingUrl(
    "mautrix-meta-helper://connect?origin=https%3A%2F%2Fbridge.example.com&id=abc&token=secret"
  );
  assert.deepEqual(parsed, { origin: "https://bridge.example.com", id: "abc", token: "secret" });
});

test("deep link rejects remote HTTP and embedded credentials", () => {
  assert.throws(() => validatePairingUrl(
    "mautrix-meta-helper://connect?origin=http%3A%2F%2Fbridge.example.com&id=abc&token=secret"
  ), /HTTPS/);
  assert.throws(() => validatePairingUrl(
    "mautrix-meta-helper://connect?origin=https%3A%2F%2Fuser%3Apass%40bridge.example.com&id=abc&token=secret"
  ), /origin/);
});

test("localhost HTTP is accepted only for local development", () => {
  assert.equal(validatePairingUrl(
    "mautrix-meta-helper://connect?origin=http%3A%2F%2Flocalhost%3A8080&id=a&token=b"
  ).origin, "http://localhost:8080");
  assert.throws(() => validatePairingUrl(
    "mautrix-meta-helper://connect?origin=http%3A%2F%2F192.168.1.5%3A8080&id=a&token=b"
  ), /HTTPS/);
});

test("Meta navigation allowlist blocks lookalike and non-HTTPS domains", () => {
  for (const url of [
    "https://facebook.com/",
    "https://www.facebook.com/login",
    "https://m.facebook.com/",
    "https://messenger.com/",
    "https://www.messenger.com/t/1",
    "https://www.fbsbx.com/captcha/recaptcha/iframe/?locale=en_US",
  ]) assert.equal(allowedMetaNavigation(url), true, url);

  for (const url of [
    "http://facebook.com/",
    "https://facebook.com.evil.example/",
    "https://evilfacebook.com/",
    "https://messenger.com.evil.example/",
    "javascript:alert(1)",
    "not-a-url",
  ]) assert.equal(allowedMetaNavigation(url), false, url);
});

test("cookie field normalization ignores malformed entries", () => {
  assert.deepEqual(cookieFields({ cookies: { fields: [
    { id: "c_user", required: true },
    null,
    { id: 123 },
    { name: "missing id" },
    { id: "xs", required: true },
  ] } }).map((f) => f.id), ["c_user", "xs"]);
});

test("Messenger Lite interactive reCAPTCHA contract is recognized narrowly", () => {
  const step = {
    type: "cookies",
    step_id: "fi.mau.meta.messengerlite.recaptcha",
    cookies: {
      url: "https://www.fbsbx.com/captcha/recaptcha/iframe/?locale=en_US",
      fields: [{
        id: "recaptcha_token",
        required: true,
        sources: [{ type: "special", name: "recaptcha_token" }],
      }],
      extract_js: "new Promise(resolve => resolve({recaptcha_token: 'abc'}))",
    },
  };
  assert.equal(isMessengerLiteRecaptchaStep(step), true);
  assert.equal(isMessengerLiteRecaptchaStep({ ...step, step_id: "other" }), false);
  assert.equal(isMessengerLiteRecaptchaStep({
    ...step,
    cookies: { ...step.cookies, url: "https://evil.example/" },
  }), false);
});

test("special challenge values are filtered to declared fields", () => {
  const step = {
    cookies: {
      fields: [{ id: "recaptcha_token", required: true }],
    },
  };
  assert.deepEqual(
    sanitizeExtractedValues(step, { recaptcha_token: "token-123", unexpected: "drop-me" }),
    { recaptcha_token: "token-123" },
  );
  assert.throws(() => sanitizeExtractedValues(step, {}), /required fields/);
});

test("completion regex is compiled and invalid patterns fail closed", () => {
  const re = completionPattern("^https://www\\.facebook\\.com/");
  assert.equal(re.test("https://www.facebook.com/messages"), true);
  assert.equal(re.test("https://evil.example/"), false);
  assert.throws(() => completionPattern("["), /invalid completion/);
});

test("protocol URL detection ignores unrelated argv", () => {
  assert.equal(findProtocolUrl(["electron", ".", "--flag"]), "");
  assert.equal(findProtocolUrl([
    "electron", ".", "mautrix-meta-helper://connect?origin=https%3A%2F%2Fx.example&id=a&token=b"
  ]).startsWith("mautrix-meta-helper://"), true);
});
