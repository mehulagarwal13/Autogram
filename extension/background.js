// Autogram extension — background service worker.
//
// Owns everything the content script must never touch directly: the JWT
// (kept only here, in chrome.storage.local — never exposed to page-context
// JS), every backend API call (a service worker's fetch() isn't subject to
// page-origin CORS the way a content script's would be, so all backend
// traffic is routed through here), and the incognito guard from the
// reference doc this extension is built from.
//
// The content script (content-script.js) only ever talks to THIS file via
// chrome.runtime messages — it never calls the backend on its own.

const DEFAULTS = {
  backendUrl: "http://127.0.0.1:8000",
  frontendUrl: "http://localhost:5173",
  autopilotEnabled: false, // mirrors ApplicationStartRequest.autopilot_enabled's own default
};

async function getConfig() {
  const stored = await chrome.storage.local.get(["backendUrl", "frontendUrl", "autopilotEnabled", "token", "email"]);
  return { ...DEFAULTS, ...stored };
}

async function setConfig(partial) {
  await chrome.storage.local.set(partial);
}

// ---------------------------------------------------------------------------
// Auth — a plain stateless JWT API. POST /auth/login takes a form-encoded
// body (OAuth2PasswordRequestForm on the backend, not JSON), so this is the
// one call that's shaped differently from everything else.
// ---------------------------------------------------------------------------

async function login(email, password) {
  const { backendUrl } = await getConfig();
  const body = new URLSearchParams({ username: email, password });
  const res = await fetch(`${backendUrl}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `Login failed (${res.status})`);
  }
  const data = await res.json();
  await setConfig({ token: data.access_token, email: data.email });
  return data;
}

async function logout() {
  await chrome.storage.local.remove(["token", "email"]);
}

async function authFetch(path, options = {}) {
  const { backendUrl, token } = await getConfig();
  if (!token) throw new Error("Not logged in. Open the popup and sign in first.");
  const headers = { ...(options.headers || {}), Authorization: `Bearer ${token}` };
  const res = await fetch(`${backendUrl}${path}`, { ...options, headers });
  if (res.status === 401) {
    await logout();
    throw new Error("Session expired — please sign in again.");
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* no body */
  }
  if (!res.ok) {
    const detail = data?.detail;
    throw new Error(typeof detail === "string" ? detail : `Request failed (${res.status})`);
  }
  return data;
}

// ---------------------------------------------------------------------------
// Incognito guard — ported from the reference fix doc. This extension must
// NEVER operate from an Incognito window: job sites that require login rely
// on the user's existing, already-logged-in session/cookies, and an
// Incognito window starts with none. No "incognito" key exists in
// manifest.json, and every entry point that touches a real tab checks this
// first and refuses rather than silently proceeding.
// ---------------------------------------------------------------------------

async function getSafeActiveTab() {
  const currentWindow = await chrome.windows.getCurrent();
  if (currentWindow.incognito) {
    throw new Error(
      "This extension does not run in Incognito windows (job sites need your real, logged-in session). " +
      "Please open a normal Chrome window and try again."
    );
  }
  const [tab] = await chrome.tabs.query({ active: true, windowId: currentWindow.id });
  if (!tab) throw new Error("No active tab found.");
  return tab;
}

// ---------------------------------------------------------------------------
// The apply flow: scan -> start application -> map fields -> fill ->
// (content script handles the CAPTCHA wait + resume-file reminder itself,
// then reports back) -> report-status. Copilot only for v1 — this code path
// never clicks Submit; see content-script.js.
// ---------------------------------------------------------------------------

async function sendToContentScript(tabId, message) {
  return chrome.tabs.sendMessage(tabId, message);
}

async function fillCurrentTab(onProgress) {
  const tab = await getSafeActiveTab();

  await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content-script.js"] });

  onProgress?.("Scanning the page...");
  const scan = await sendToContentScript(tab.id, { type: "SCAN_PAGE" });
  if (scan.error) throw new Error(scan.error);

  onProgress?.("Starting the application...");
  const { autopilotEnabled } = await getConfig();
  const application = await authFetch("/applications/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      job_url: scan.jobUrl,
      source: "browser_extension",
      autopilot_enabled: autopilotEnabled,
      company: scan.company || undefined,
      position: scan.title || undefined,
    }),
  });

  onProgress?.("Mapping fields from your profile...");
  const mapped = scan.fields.length
    ? await authFetch("/automation/map-fields", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          application_id: application.application_id,
          page_number: 1,
          fields: scan.fields.map((f) => ({
            question_text: f.questionText,
            field_type: f.fieldType,
            options: f.options,
          })),
        }),
      })
    : [];

  onProgress?.("Filling in what we can...");
  const fillResult = await sendToContentScript(tab.id, {
    type: "FILL_FIELDS",
    results: mapped,
  });

  if (fillResult.captchaReason) {
    await authFetch(`/applications/${application.application_id}/report-status`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: "manual_required", reason: fillResult.captchaReason }),
    });
    onProgress?.("Waiting for you to complete verification on the page...");
    const cleared = await sendToContentScript(tab.id, { type: "WAIT_FOR_CAPTCHA_CLEAR" });
    if (!cleared.cleared) {
      await authFetch(`/applications/${application.application_id}/report-status`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "manual_required", reason: "Still waiting on human verification." }),
      });
      return { applicationId: application.application_id, status: "manual_required", frontendUrl: (await getConfig()).frontendUrl };
    }
  }

  const lowConfidenceCount = mapped.filter((m) => m.confidence_level === "LOW").length;
  const finalStatus = lowConfidenceCount > 0 || fillResult.missingRequired?.length ? "needs_review" : "copilot_review";
  await authFetch(`/applications/${application.application_id}/report-status`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      status: finalStatus,
      confidence: mapped.length ? mapped.reduce((sum, m) => sum + m.confidence, 0) / mapped.length : null,
      reason: fillResult.resumeFieldFound ? "Attach your résumé manually — browsers block scripts from setting a file input." : null,
    }),
  });

  const { frontendUrl } = await getConfig();
  return {
    applicationId: application.application_id,
    status: finalStatus,
    resumeFieldFound: fillResult.resumeFieldFound,
    lowConfidenceCount,
    frontendUrl,
  };
}

// ---------------------------------------------------------------------------
// Message router — the popup's only way to reach any of the above.
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  (async () => {
    try {
      switch (message.type) {
        case "GET_CONFIG":
          sendResponse({ ok: true, config: await getConfig() });
          break;
        case "SET_CONFIG":
          await setConfig(message.config);
          sendResponse({ ok: true });
          break;
        case "LOGIN":
          sendResponse({ ok: true, data: await login(message.email, message.password) });
          break;
        case "LOGOUT":
          await logout();
          sendResponse({ ok: true });
          break;
        case "FILL_THIS_APPLICATION":
          sendResponse({ ok: true, result: await fillCurrentTab() });
          break;
        default:
          sendResponse({ ok: false, error: `Unknown message type: ${message.type}` });
      }
    } catch (e) {
      sendResponse({ ok: false, error: e.message || String(e) });
    }
  })();
  return true; // keep the message channel open for the async response
});
