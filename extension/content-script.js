// Autogram extension — content script.
//
// The ONLY part of this extension that touches the live DOM. Never calls the
// backend directly (everything goes through background.js — see its
// docstring). Injected on demand via chrome.scripting.executeScript, so this
// file's top-level state (the `_scannedFields` map) lives for as long as the
// tab isn't navigated/reloaded, which is exactly the lifetime one apply-flow
// message exchange needs.
//
// Mirrors (in plain JS, since a content script cannot call Python) several
// server-side heuristics from automation/browser/selectors.py — kept as
// close as practical so a future change to the Python lists is easy to
// notice needs mirroring here too:
//   - CAPTCHA_HINTS / _HUMAN_GATES        -> CAPTCHA_HINTS / HUMAN_GATE_HINTS below
//   - find_apply_entry_button              -> findApplyEntryButton below
//   - find_job_posting_title_and_company    -> scanJobPostingMetadata below
//   - find_submit_button                     -> findSubmitButton below
//   - find_submission_confirmation             -> findSubmissionConfirmation below
//
// Fill order matches this session's server-side fix: fields are filled
// FIRST, CAPTCHA is checked LAST, right before reporting the page done — a
// CAPTCHA widget on a real ATS form typically gates submission, not the
// fields themselves.
//
// CLICK_SUBMIT is only ever sent by background.js after the server's OWN
// decide_action() (via POST /automation/map-fields) returned AUTO_SUBMIT —
// this file never decides on its own whether a form should be submitted.

const _scannedFields = []; // [{ el, questionText, fieldType, options }]

// --- CAPTCHA / human-gate detection (mirrors selectors.py) -----------------

const CAPTCHA_HINTS = ["captcha", "recaptcha", "hcaptcha", "cf-turnstile", "turnstile"];

function isVisible(el) {
  const rect = el.getBoundingClientRect();
  if (rect.width < 1 && rect.height < 1) return false;
  const style = window.getComputedStyle(el);
  return style.visibility !== "hidden" && style.display !== "none";
}

function pageHasCaptcha() {
  for (const hint of CAPTCHA_HINTS) {
    const candidates = document.querySelectorAll(
      `iframe[src*="${hint}" i], [class*="${hint}" i], [id*="${hint}" i]`
    );
    for (const el of candidates) {
      if (isVisible(el)) return true;
    }
  }
  return false;
}

const HUMAN_GATE_HINTS = [
  { name: "login or registration required", selector: "input[type='password']" },
  { name: "one-time passcode / multi-factor authentication", selector: "input[autocomplete='one-time-code'], input[name*='otp' i], input[id*='otp' i]" },
];

function findHumanGate() {
  for (const gate of HUMAN_GATE_HINTS) {
    const candidates = document.querySelectorAll(gate.selector);
    for (const el of candidates) {
      if (isVisible(el)) return gate.name;
    }
  }
  return null;
}

// --- Job posting metadata (mirrors find_job_posting_title_and_company) ----

function scanJobPostingMetadata() {
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const data = JSON.parse(script.textContent);
      const entries = Array.isArray(data) ? data : [data];
      for (const entry of entries) {
        const types = Array.isArray(entry["@type"]) ? entry["@type"] : [entry["@type"]];
        if (!types.some((t) => String(t).toLowerCase() === "jobposting")) continue;
        const title = typeof entry.title === "string" ? entry.title.trim() : null;
        const org = entry.hiringOrganization;
        const company = typeof org === "string" ? org.trim() : org?.name?.trim?.() || null;
        if (title || company) return { title: title || null, company: company || null };
      }
    } catch {
      /* malformed JSON-LD — ignore, never guess */
    }
  }
  const ogTitle = document.querySelector('meta[property="og:title"]')?.getAttribute("content")?.trim() || null;
  const ogSite = document.querySelector('meta[property="og:site_name"]')?.getAttribute("content")?.trim() || null;
  return { title: ogTitle, company: ogSite };
}

// --- Apply-entry button (mirrors find_apply_entry_button) -----------------

const APPLY_ENTRY_TEXT_CANDIDATES = [
  "apply now", "apply for this job", "apply for this position", "start application",
  "start your application", "begin application", "apply online", "apply today", "apply",
];
const THIRD_PARTY_AUTOFILL_RE = /linkedin|indeed|glassdoor|\bgoogle\b|\bgithub\b|sign\s*in|sign\s*up|log\s*in|create\s+an?\s+account/i;

function findApplyEntryButton() {
  const clickable = document.querySelectorAll("button, a, input[type='submit'], input[type='button']");
  for (const candidate of APPLY_ENTRY_TEXT_CANDIDATES) {
    for (const el of clickable) {
      const text = (el.innerText || el.value || "").trim().toLowerCase();
      if (!text || THIRD_PARTY_AUTOFILL_RE.test(text)) continue;
      if (text === candidate || text.includes(candidate)) {
        if (isVisible(el) && !el.disabled) return el;
      }
    }
  }
  return null;
}

// --- Submit button + confirmation (mirrors find_submit_button /
// find_submission_confirmation in automation/browser/selectors.py) --------
// Only ever called when background.js has already confirmed the server's
// decide_action() returned AUTO_SUBMIT for this application — this file
// never decides on its own whether to submit.

const SUBMIT_TEXT_CANDIDATES = ["submit application", "submit", "apply", "send application", "send"];

function findSubmitButton() {
  const clickable = document.querySelectorAll("button, a, input[type='submit'], input[type='button']");
  for (const candidate of SUBMIT_TEXT_CANDIDATES) {
    for (const el of clickable) {
      const text = (el.innerText || el.value || "").trim().toLowerCase();
      if (!text || THIRD_PARTY_AUTOFILL_RE.test(text)) continue;
      if (text === candidate && isVisible(el) && !el.disabled) return el;
    }
  }
  return null;
}

const SUBMISSION_CONFIRMATION_URL_HINTS = ["thank", "confirmation", "confirmed", "/success", "submitted"];
const SUBMISSION_CONFIRMATION_TEXT_PATTERNS = [
  "thank you for applying", "thanks for applying", "thank you for your application",
  "thank you for your interest", "application received", "we have received your application",
  "we've received your application", "your application has been received",
  "application submitted", "your application has been submitted", "successfully submitted",
  "application complete",
];
const APPLICATION_REFERENCE_RE = /(application|reference|confirmation)\s*(id|number|no\.?|#)\s*[:\-#]?\s*[a-z0-9][a-z0-9-]{2,}/i;

function findSubmissionConfirmation() {
  const url = (window.location.href || "").toLowerCase();
  for (const hint of SUBMISSION_CONFIRMATION_URL_HINTS) {
    if (url.includes(hint)) return `confirmation URL (matched "${hint}")`;
  }
  const text = (document.body.innerText || "").toLowerCase();
  for (const pattern of SUBMISSION_CONFIRMATION_TEXT_PATTERNS) {
    if (text.includes(pattern)) return `success message ("${pattern}")`;
  }
  const reference = text.match(APPLICATION_REFERENCE_RE);
  if (reference) return `application reference ("${reference[0]}")`;
  return null;
}

async function handleClickSubmit() {
  const button = findSubmitButton();
  if (!button) return { clicked: false, confirmed: false, detail: "No submit control could be found on this page." };

  button.click();
  // Poll for confirmation the same way wait_for_submission_confirmation
  // does server-side — a landed click is NEVER itself reported as
  // confirmed; only positive evidence (URL/text/reference) counts.
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    const confirmation = findSubmissionConfirmation();
    if (confirmation) return { clicked: true, confirmed: true, detail: confirmation };
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return {
    clicked: true, confirmed: false,
    detail: "Submit was clicked but no confirmation could be detected — verify on the site before retrying (retrying a submission that did succeed would double-apply).",
  };
}

// --- Field scanning ----------------------------------------------------

function labelFor(el) {
  if (el.id) {
    const byFor = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (byFor?.innerText?.trim()) return byFor.innerText.trim();
  }
  const ariaLabel = el.getAttribute("aria-label");
  if (ariaLabel?.trim()) return ariaLabel.trim();
  const closestLabel = el.closest("label");
  if (closestLabel?.innerText?.trim()) return closestLabel.innerText.trim();
  if (el.placeholder?.trim()) return el.placeholder.trim();
  // Nearest preceding text as a last resort — best-effort, never fabricated.
  let node = el.previousElementSibling;
  for (let i = 0; i < 3 && node; i++, node = node.previousElementSibling) {
    const text = node.innerText?.trim();
    if (text && text.length < 200) return text;
  }
  return el.name || el.id || "";
}

function scanFields() {
  const results = [];
  const seenRadioGroups = new Set();
  const controls = document.querySelectorAll(
    "input:not([type='hidden']):not([type='submit']):not([type='button']):not([type='file']), select, textarea"
  );

  for (const el of controls) {
    if (!isVisible(el)) continue;
    const type = (el.type || "").toLowerCase();
    if (type === "radio") {
      if (!el.name || seenRadioGroups.has(el.name)) continue;
      seenRadioGroups.add(el.name);
      const group = document.querySelectorAll(`input[type='radio'][name="${CSS.escape(el.name)}"]`);
      const options = Array.from(group).map((r) => labelFor(r)).filter(Boolean);
      _scannedFields.push({ el, questionText: labelFor(el) || el.name, fieldType: "radio", options });
      continue;
    }
    if (type === "checkbox") {
      _scannedFields.push({ el, questionText: labelFor(el), fieldType: "checkbox", options: ["Yes", "No"] });
      continue;
    }
    let options = null;
    if (el.tagName === "SELECT") {
      options = Array.from(el.options).map((o) => o.text.trim()).filter((t) => t);
    }
    _scannedFields.push({
      el,
      questionText: labelFor(el),
      fieldType: el.tagName === "TEXTAREA" ? "textarea" : el.tagName === "SELECT" ? "select" : type || "text",
      options,
    });
  }

  for (const f of _scannedFields) {
    results.push({ questionText: f.questionText, fieldType: f.fieldType, options: f.options });
  }
  return results;
}

function hasFileInput() {
  return document.querySelectorAll("input[type='file']").length > 0;
}

// --- Light-touch ATS platform hint ---------------------------------------
// The backend's own pre-flight `detect_ats_for_url` (app/api/applications.py)
// only ever sees the URL the user pasted — for "Apply from Job Link" flows
// where clicking Apply reveals the real ATS in place (see
// findApplyEntryButton above), it never gets a chance to see what THIS
// content script already sees post-click. A small, honest hint — not a full
// reimplementation of automation/ats/detector.py's tiered detection — lets
// the backend's decide_action() see the real platform instead of "custom"
// (which decide_action always refuses to auto-submit on, since it isn't in
// PUBLIC_ATS_PLATFORMS). Only ever a HINT: the backend still owns the actual
// adapter-registry/public-ATS-allowlist decision.
function detectAtsPlatformHint() {
  const url = window.location.href.toLowerCase();
  if (document.getElementById("grnhse_app") || url.includes("greenhouse.io") || url.includes("job-boards.greenhouse")) return "greenhouse";
  if (document.querySelector("[data-qa='btn-apply']") || url.includes("lever.co")) return "lever";
  return null;
}

// --- Message handlers -------------------------------------------------

async function handleScanPage() {
  const metadata = scanJobPostingMetadata();
  let fields = scanFields();

  // No form fields at all yet — this looks like a job LISTING page, not a
  // form. Try the Apply/Apply Now/Start Application control once (same-tab
  // case only for v1 — a click that opens a NEW TAB isn't followed here;
  // the user is told to click it themselves in that case, see below).
  if (fields.length === 0) {
    const applyButton = findApplyEntryButton();
    if (applyButton) {
      applyButton.click();
      await new Promise((resolve) => setTimeout(resolve, 2000));
      _scannedFields.length = 0;
      fields = scanFields();
    }
  }

  if (fields.length === 0) {
    return {
      error:
        "No fillable form found on this page, and no Apply/Apply Now/Start Application button could be " +
        "clicked automatically (it may open a new tab — please click it yourself, then try again).",
    };
  }

  return {
    jobUrl: window.location.href, company: metadata.company, title: metadata.title, fields,
    atsPlatformHint: detectAtsPlatformHint(),
  };
}

function dispatchInputEvents(el) {
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

const HIGH_MEDIUM_CONFIDENCE_LEVELS = new Set(["HIGH", "MEDIUM"]);

function handleFillFields(results) {
  const byQuestion = new Map(results.map((r) => [r.question_text, r]));

  for (const field of _scannedFields) {
    const result = byQuestion.get(field.questionText);
    if (!result || !result.answer) continue;
    if (!HIGH_MEDIUM_CONFIDENCE_LEVELS.has(result.confidence_level)) continue; // LOW stays empty, flagged for review

    const el = field.el;
    if (field.fieldType === "checkbox") {
      el.checked = /^(yes|true|1)$/i.test(result.answer);
      dispatchInputEvents(el);
    } else if (field.fieldType === "radio") {
      const group = document.querySelectorAll(`input[type='radio'][name="${CSS.escape(el.name)}"]`);
      for (const radio of group) {
        if (labelFor(radio).trim().toLowerCase() === result.answer.trim().toLowerCase()) {
          radio.checked = true;
          dispatchInputEvents(radio);
          break;
        }
      }
    } else if (field.fieldType === "select") {
      const option = Array.from(el.options).find((o) => o.text.trim().toLowerCase() === result.answer.trim().toLowerCase());
      if (option) {
        el.value = option.value;
        dispatchInputEvents(el);
      }
    } else {
      el.value = result.answer;
      dispatchInputEvents(el);
    }
  }

  // Résumé upload: a content script cannot programmatically set a file
  // input's value — browsers forbid it for security, the one thing
  // server-side Playwright (via CDP) can do that this genuinely can't. The
  // human attaches it manually; this just flags that a field exists.
  const resumeFieldFound = hasFileInput();

  const missingRequired = _scannedFields
    .filter((f) => f.el.required && !f.el.value)
    .map((f) => f.questionText);

  // CAPTCHA is checked LAST, after everything fillable has been attempted —
  // matches ApplicationFlowManager._process_page's ordering exactly.
  if (pageHasCaptcha()) {
    return { captchaReason: "CAPTCHA present — please complete it in this tab.", resumeFieldFound, missingRequired };
  }
  const gate = findHumanGate();
  if (gate) {
    return { captchaReason: `Human intervention required: ${gate}.`, resumeFieldFound, missingRequired };
  }

  return { resumeFieldFound, missingRequired };
}

const CAPTCHA_WAIT_TIMEOUT_MS = 10 * 60 * 1000; // mirrors AUTOMATION_HUMAN_WAIT_TIMEOUT_S default
const CAPTCHA_POLL_INTERVAL_MS = 5000;

async function handleWaitForCaptchaClear() {
  const deadline = Date.now() + CAPTCHA_WAIT_TIMEOUT_MS;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, CAPTCHA_POLL_INTERVAL_MS));
    if (!pageHasCaptcha() && !findHumanGate()) return { cleared: true };
  }
  return { cleared: false };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  (async () => {
    switch (message.type) {
      case "SCAN_PAGE":
        sendResponse(await handleScanPage());
        break;
      case "FILL_FIELDS":
        sendResponse(handleFillFields(message.results));
        break;
      case "WAIT_FOR_CAPTCHA_CLEAR":
        sendResponse(await handleWaitForCaptchaClear());
        break;
      case "CLICK_SUBMIT":
        sendResponse(await handleClickSubmit());
        break;
      default:
        sendResponse({ error: `Unknown message type: ${message.type}` });
    }
  })();
  return true;
});
