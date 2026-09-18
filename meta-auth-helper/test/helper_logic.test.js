"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  findProtocolUrl,
  validatePairingUrl,
  cookieFields,
  allowedMetaNavigation,
  cookieSourceNames,
  interactiveExtractScript,
  normalizeExtractedValues,
  completionPattern,
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

test("challenge navigation allows Meta and known reCAPTCHA origins only", () => {
  for (const url of [
    "https://www.fbsbx.com/captcha/recaptcha/iframe/",
    "https://www.google.com/recaptcha/api2/anchor",
    "https://www.recaptcha.net/recaptcha/api2/anchor",
  ]) assert.equal(allowedMetaNavigation(url), true, url);

  for (const url of [
    "https://google.com.evil.example/recaptcha",
    "https://evil.example/captcha",
    "https://www.google.com/",
    "https://www.fbsbx.com/",
  ]) assert.equal(allowedMetaNavigation(url), false, url);
});

test("cookie source names honor BridgeV2 source metadata", () => {
  assert.deepEqual(cookieSourceNames({
    id: "session",
    sources: [{ type: "cookie", name: "xs" }, { type: "cookie", name: "c_user" }],
  }), ["xs", "c_user"]);
  assert.deepEqual(cookieSourceNames({ id: "legacy" }), ["legacy"]);
});

test("interactive reCAPTCHA extractor is enabled only for special recaptcha field", () => {
  const step = {
    type: "cookies",
    cookies: {
      extract_js: "new Promise(resolve => window.done = resolve)",
      fields: [{
        id: "recaptcha_token",
        required: true,
        sources: [{ type: "special", name: "recaptcha_token" }],
      }],
    },
  };
  assert.equal(interactiveExtractScript(step), step.cookies.extract_js);
  assert.equal(interactiveExtractScript({
    type: "cookies",
    cookies: { extract_js: "danger()", fields: [{ id: "xs" }] },
  }), "");
});

test("interactive extraction keeps only requested non-empty strings", () => {
  const step = {
    cookies: {
      fields: [{
        id: "recaptcha_token",
        required: true,
        sources: [{ type: "special", name: "recaptcha_token" }],
      }],
    },
  };
  assert.deepEqual(
    normalizeExtractedValues(step, { recaptcha_token: "token", unexpected: "drop" }),
    { values: { recaptcha_token: "token" }, missing: [] }
  );
  assert.deepEqual(
    normalizeExtractedValues(step, { recaptcha_token: "" }),
    { values: {}, missing: ["recaptcha_token"] }
  );
  assert.throws(() => normalizeExtractedValues(step, "token"), /field map/);
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
