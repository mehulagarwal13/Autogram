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

// Clicking the toolbar icon opens the side panel (src/sidepanel/) instead of
// a popup — a persistent panel that stays open alongside the job page, which
// is the whole point of an in-extension review UI.
chrome.sidePanel?.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});

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
// Client-side deterministic profile mapping — the obvious identity fields
// (name/email/phone/address/LinkedIn/...) are matched straight from the
// already-fetched GET /profile response, with NO backend call and NO LLM
// involved, exactly like automation/ats/base.py's FieldMapper resolves them
// server-side before ApplicationAnswerEngine ever sees a field. Only
// genuinely unmapped fields (screening questions, subjective prompts) go to
// POST /automation/map-fields. Cheaper, and matches the server path's own
// division of labor instead of routing every trivial field through an LLM.
// ---------------------------------------------------------------------------

const DETERMINISTIC_FIELD_PATTERNS = [
  { re: /\bfirst\s*name\b/i, key: "first_name" },
  { re: /\blast\s*name\b|surname/i, key: "last_name" },
  { re: /\bfull\s*name\b|\byour\s*name\b|^name$/i, key: "full_name" },
  { re: /\be-?mail\b/i, key: "email" },
  { re: /\bphone\b|\bmobile\b|\bcontact\s*number\b/i, key: "phone" },
  { re: /\baddress\b/i, key: "address" },
  { re: /\bcity\b/i, key: "city" },
  { re: /\bstate\b|\bprovince\b/i, key: "state" },
  { re: /\bpostal\s*code\b|\bzip\b/i, key: "postal_code" },
  { re: /\bcountry\b/i, key: "country" },
  { re: /linkedin/i, key: "linkedin_url" },
  { re: /github/i, key: "github_url" },
  { re: /portfolio/i, key: "portfolio_url" },
  { re: /\bwebsite\b/i, key: "website_url" },
  { re: /current\s*company/i, key: "current_company" },
  { re: /current\s*(role|title|position)/i, key: "current_role" },
];

function mapFieldsDeterministically(fields, profile) {
  const matched = [];
  const remaining = [];
  for (const field of fields) {
    const hit = DETERMINISTIC_FIELD_PATTERNS.find((p) => p.re.test(field.questionText || ""));
    const value = hit ? profile?.[hit.key] : null;
    if (hit && value) {
      matched.push({ question_text: field.questionText, answer: String(value), confidence: 1.0, confidence_level: "HIGH", source: "profile" });
    } else {
      remaining.push(field);
    }
  }
  return { matched, remaining };
}

let _profileCache = null;
async function getProfile() {
  if (_profileCache) return _profileCache;
  _profileCache = await authFetch("/profile");
  return _profileCache;
}

// ---------------------------------------------------------------------------
// The shared "policy brain" — GET /automation/config. Polled fresh (never
// cached for a session) before every fill AND again before any auto-submit
// click, exactly like the server-side Playwright engine's own kill-switch
// check. Same fail-closed contract: a broken call is treated as "engaged."
// ---------------------------------------------------------------------------

async function getAutomationConfig() {
  try {
    return await authFetch("/automation/config");
  } catch (e) {
    return {
      kill_switch_engaged: true,
      pacing: null,
      auto_submit_confidence_threshold: 0.85,
      needs_review_confidence_threshold: 0.6,
      public_ats_platforms: [],
      _fetchError: e.message || String(e),
    };
  }
}

// ---------------------------------------------------------------------------
// Daily cap / working-hours pacing — client-side, best-effort (see
// extension/README.md: a determined user could disable their own
// extension's pacing; the real backstop is that decide_action() — server-
// authoritative — is what actually gates auto-submit regardless). Reads its
// numbers from GET /automation/config's `pacing`, never hardcoded.
// ---------------------------------------------------------------------------

function todayKey() {
  return `applyCount:${new Date().toISOString().slice(0, 10)}`;
}

async function checkPacingAllowed(pacing) {
  if (!pacing) return { allowed: true }; // config fetch failed — kill-switch fail-closed already handles safety
  const hour = new Date().getHours();
  if (hour < pacing.working_hours_start || hour >= pacing.working_hours_end) {
    return { allowed: false, reason: `Outside the configured working hours (${pacing.working_hours_start}:00–${pacing.working_hours_end}:00).` };
  }
  const key = todayKey();
  const stored = await chrome.storage.local.get([key]);
  const countToday = stored[key] || 0;
  if (countToday >= pacing.daily_application_cap) {
    return { allowed: false, reason: `Daily application cap reached (${pacing.daily_application_cap}/day).` };
  }
  return { allowed: true, key, countToday };
}

async function recordApplicationStarted(key, countToday) {
  if (!key) return;
  await chrome.storage.local.set({ [key]: countToday + 1 });
}

// ---------------------------------------------------------------------------
// The apply flow: kill-switch + pacing check -> scan -> start application ->
// map fields (returns overall_confidence + action from the server's OWN
// decide_action(), never reimplemented here) -> fill -> CAPTCHA wait if
// needed -> AUTO_SUBMIT clicks Submit itself (re-checking the kill switch
// live, immediately before), anything else stops for the human -> report-status.
// ---------------------------------------------------------------------------

async function sendToContentScript(tabId, message) {
  return chrome.tabs.sendMessage(tabId, message);
}

async function fillCurrentTab(onProgress) {
  const tab = await getSafeActiveTab();

  onProgress?.("Checking kill switch and pacing limits...");
  const config = await getAutomationConfig();
  if (config.kill_switch_engaged) {
    throw new Error(
      config._fetchError
        ? `Could not verify the kill switch is off — refusing to proceed (${config._fetchError}).`
        : "The autopilot kill switch is engaged for your account — re-enable it in Settings to continue."
    );
  }
  const pacing = await checkPacingAllowed(config.pacing);
  if (!pacing.allowed) throw new Error(pacing.reason);

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
      ats_platform_hint: scan.atsPlatformHint || undefined,
    }),
  });
  await recordApplicationStarted(pacing.key, pacing.countToday);

  onProgress?.("Matching obvious fields from your profile...");
  const profile = await getProfile().catch(() => null);
  const { matched: deterministicMatches, remaining } = mapFieldsDeterministically(scan.fields, profile);

  onProgress?.("Mapping the rest via profile history + AI...");
  const backendMapped = remaining.length
    ? await authFetch("/automation/map-fields", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          application_id: application.application_id,
          page_number: 1,
          fields: remaining.map((f) => ({
            question_text: f.questionText,
            field_type: f.fieldType,
            options: f.options,
          })),
        }),
      })
    : { fields: [], overall_confidence: null, action: null };

  // Recombine so overall_confidence reflects ALL fields, not just the ones
  // sent to the backend — same "fraction usable" definition
  // ApplicationFlowManager._aggregate_confidence uses. This part is plain
  // arithmetic, not a policy decision. The actual submission decision is
  // one, so it ALWAYS goes through POST /automation/decide (wrapping the
  // real decide_action()) — never computed here, even when every field
  // happened to be matched deterministically with no backend fields call.
  const allMapped = [...deterministicMatches, ...backendMapped.fields];
  const usableCount = allMapped.filter((m) => m.confidence_level === "HIGH" || m.confidence_level === "MEDIUM").length;
  const overallConfidence = allMapped.length ? usableCount / allMapped.length : 0.0;

  onProgress?.("Checking whether this clears the auto-submit bar...");
  const decision = await authFetch("/automation/decide", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ application_id: application.application_id, overall_confidence: overallConfidence }),
  });
  const mapResponse = { fields: allMapped, overall_confidence: overallConfidence, action: decision.action };
  const mapped = mapResponse.fields;

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

  const { frontendUrl } = await getConfig();
  const lowConfidenceCount = mapped.filter((m) => m.confidence_level === "LOW").length;
  const resumeReason = fillResult.resumeFieldFound
    ? "Attach your résumé manually — browsers block scripts from setting a file input."
    : null;

  // AUTO_SUBMIT comes straight from the server's own decide_action() call —
  // this code never computes that decision itself. Re-check the kill switch
  // LIVE, immediately before clicking, since it may have changed since the
  // fill started (see plan Phase 2: never just check once per session).
  if (mapResponse.action === "AUTO_SUBMIT" && !resumeReason) {
    onProgress?.("Confidence and platform cleared the auto-submit bar — verifying kill switch once more...");
    const preSubmitConfig = await getAutomationConfig();
    if (!preSubmitConfig.kill_switch_engaged) {
      onProgress?.("Submitting...");
      const submitResult = await sendToContentScript(tab.id, { type: "CLICK_SUBMIT" });
      const finalStatus = submitResult.confirmed ? "applied" : submitResult.clicked ? "needs_review" : "copilot_review";
      await authFetch(`/applications/${application.application_id}/report-status`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          status: finalStatus,
          confidence: mapResponse.overall_confidence,
          reason: submitResult.confirmed ? null : (submitResult.detail || "Submit was clicked but could not be confirmed — verify on the site before retrying."),
        }),
      });
      return { applicationId: application.application_id, status: finalStatus, autoSubmitted: true, frontendUrl };
    }
    onProgress?.("Kill switch engaged just now — stopping before submit, handing this to you for review.");
  }

  const finalStatus = mapResponse.action === "NEEDS_REVIEW" || lowConfidenceCount > 0 || fillResult.missingRequired?.length
    ? "needs_review"
    : "copilot_review";
  await authFetch(`/applications/${application.application_id}/report-status`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      status: finalStatus,
      confidence: mapResponse.overall_confidence,
      reason: resumeReason,
    }),
  });

  return {
    applicationId: application.application_id,
    status: finalStatus,
    resumeFieldFound: fillResult.resumeFieldFound,
    lowConfidenceCount,
    action: mapResponse.action,
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
        case "FILL_THIS_APPLICATION": {
          const result = await fillCurrentTab((status) => {
            // Broadcast — side panel listens for live progress; a no-op if
            // nothing's listening (panel closed mid-fill).
            chrome.runtime.sendMessage({ type: "FILL_PROGRESS", status }).catch(() => {});
          });
          sendResponse({ ok: true, result });
          break;
        }
        case "GET_AUTOMATION_CONFIG":
          sendResponse({ ok: true, config: await getAutomationConfig() });
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
