"use strict";

const { app, BrowserWindow } = require("electron");
const {
  PROTOCOL,
  findProtocolUrl,
  validatePairingUrl,
  cookieFields,
  allowedMetaNavigation,
  completionPattern,
} = require("./helper_logic");

let statusWindow = null;
let authWindow = null;
let activePairing = null;
let processing = false;

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function setStatus(title, message, error = false) {
  if (!statusWindow || statusWindow.isDestroyed()) {
    statusWindow = new BrowserWindow({
      width: 520,
      height: 280,
      resizable: false,
      autoHideMenuBar: true,
      webPreferences: {
        nodeIntegration: false,
        contextIsolation: true,
        sandbox: true,
      },
    });
    statusWindow.on("closed", () => { statusWindow = null; });
  }
  const accent = error ? "#b91c1c" : "#1d4ed8";
  const html = `<!doctype html><meta charset="utf-8"><title>${escapeHtml(title)}</title>
  <style>body{font-family:system-ui,sans-serif;margin:0;padding:32px;background:#f8fafc;color:#0f172a}
  .card{background:white;border:1px solid #e2e8f0;border-radius:16px;padding:28px;box-shadow:0 8px 30px rgba(15,23,42,.08)}
  h1{font-size:22px;margin:0 0 12px;color:${accent}}p{line-height:1.5;margin:0;color:#475569}</style>
  <div class="card"><h1>${escapeHtml(title)}</h1><p>${escapeHtml(message)}</p></div>`;
  statusWindow.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
  statusWindow.show();
}

async function helperRequest(pairing, method, body) {
  const response = await fetch(`${pairing.origin}/api/meta/helper/${encodeURIComponent(pairing.id)}`, {
    method,
    headers: {
      Authorization: `Bearer ${pairing.token}`,
      Accept: "application/json",
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
    redirect: "error",
    signal: AbortSignal.timeout(20000),
  });
  let data = {};
  try { data = await response.json(); } catch (_) { /* normalized below */ }
  if (!response.ok) {
    throw new Error(data.error || `Integration server returned HTTP ${response.status}`);
  }
  return data;
}

async function collectRequiredCookies(session, authUrl, fields) {
  const available = await session.cookies.get({ url: authUrl });
  const values = {};
  for (const field of fields) {
    const found = available.find((cookie) => cookie.name === field.id);
    if (found && found.value) values[field.id] = found.value;
  }
  const missing = fields.filter((field) => field.required !== false && !values[field.id]).map((field) => field.id);
  return { values, missing };
}

async function beginCookieLogin(pairing, descriptor) {
  const step = descriptor.step || {};
  const params = step.cookies || {};
  const authUrl = String(params.url || "");
  if (!allowedMetaNavigation(authUrl)) throw new Error("The bridge returned an unexpected authentication origin");

  const completionRegex = completionPattern(params.wait_for_url_pattern);
  const fields = cookieFields(step);
  if (!fields.length) throw new Error("The bridge did not specify the required Meta cookies");

  if (authWindow && !authWindow.isDestroyed()) authWindow.destroy();
  const partition = `meta-auth-${pairing.id}-${Date.now()}`; // no persist: prefix => in-memory session
  authWindow = new BrowserWindow({
    width: 1100,
    height: 820,
    title: "Facebook authentication",
    autoHideMenuBar: true,
    webPreferences: {
      partition,
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      webSecurity: true,
      devTools: false,
    },
  });
  const ses = authWindow.webContents.session;
  ses.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  authWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  authWindow.webContents.on("will-navigate", (event, url) => {
    if (!allowedMetaNavigation(url)) event.preventDefault();
  });
  authWindow.webContents.on("will-redirect", (event, url) => {
    if (!allowedMetaNavigation(url)) event.preventDefault();
  });
  if (params.user_agent) authWindow.webContents.setUserAgent(String(params.user_agent));

  let submitting = false;
  const maybeComplete = async (url) => {
    if (submitting || !completionRegex.test(url)) return;
    const captured = await collectRequiredCookies(ses, authUrl, fields);
    if (captured.missing.length) return;
    submitting = true;
    setStatus("Validando la sesión", "Facebook terminó el acceso. Estamos entregando la sesión directamente al bridge.");
    try {
      const result = await helperRequest(pairing, "POST", { cookies: captured.values });
      for (const key of Object.keys(captured.values)) captured.values[key] = "";
      if (result.complete) {
        setStatus("Cuenta conectada", "La sesión fue aceptada. Puedes volver al panel de administración.");
        if (authWindow && !authWindow.isDestroyed()) authWindow.close();
      } else {
        setStatus("Paso adicional requerido", `El bridge solicitó el siguiente paso: ${result.next_step || "desconocido"}. Vuelve al panel para continuar.`);
        if (authWindow && !authWindow.isDestroyed()) authWindow.close();
      }
    } catch (error) {
      for (const key of Object.keys(captured.values)) captured.values[key] = "";
      setStatus("No se pudo completar el acceso", error.message || String(error), true);
      if (authWindow && !authWindow.isDestroyed()) authWindow.close();
    }
  };

  authWindow.webContents.on("did-navigate", (_event, url) => { void maybeComplete(url); });
  authWindow.webContents.on("did-navigate-in-page", (_event, url) => { void maybeComplete(url); });
  authWindow.on("closed", () => { authWindow = null; });

  setStatus("Inicia sesión en Facebook", "Completa el acceso, 2FA o cualquier checkpoint dentro de la ventana segura que acaba de abrirse.");
  await authWindow.loadURL(authUrl);
  authWindow.show();
}

async function handleProtocol(raw) {
  if (processing) return;
  processing = true;
  try {
    const pairing = validatePairingUrl(raw);
    activePairing = pairing;
    setStatus("Conectando con el servidor", "Validando el emparejamiento de un solo uso…");
    const descriptor = await helperRequest(pairing, "GET");
    if (descriptor.type !== "meta-cookie-login" || descriptor.step?.type !== "cookies") {
      throw new Error("This helper session is not a cookie-login step");
    }
    await beginCookieLogin(pairing, descriptor);
  } catch (error) {
    setStatus("No se pudo iniciar", error.message || String(error), true);
  } finally {
    processing = false;
  }
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", (_event, argv) => {
    const url = findProtocolUrl(argv);
    if (url) void handleProtocol(url);
    if (statusWindow) statusWindow.focus();
  });

  app.on("open-url", (event, url) => {
    event.preventDefault();
    if (url.startsWith(`${PROTOCOL}://`)) void handleProtocol(url);
  });

  app.whenReady().then(() => {
    app.setAsDefaultProtocolClient(PROTOCOL);
    setStatus("Meta Connection Helper", "Abre /admin → Facebook Messenger y pulsa “Abrir helper de Facebook”.");
    const initial = findProtocolUrl(process.argv);
    if (initial) void handleProtocol(initial);
  });

  app.on("window-all-closed", () => {
    activePairing = null;
    app.quit();
  });
}
