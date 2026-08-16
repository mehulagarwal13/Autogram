// Autogram extension — content script.
//
// The ONLY part of this extension that touches the live DOM. Never calls the
// backend directly (everything goes through background.js — see its
// docstring). Injected on demand via chrome.scripting.executeScript, so this
// file's top-level state (the `_scannedFields` map) lives for as long as the
// tab isn't navigated/reloaded, which is exactly the lifetime one apply-flow
// message exchange needs.
//
// Mirrors (in plain JS, since a content script cannot call Python) two
// server-side heuristics from automation/browser/selectors.py — kept as
// close as practical so a future change to the Python lists is easy to
// notice needs mirroring here too:
//   - CAPTCHA_HINTS / _HUMAN_GATES  -> CAPTCHA_HINTS / HUMAN_GATE_HINTS below
//   - find_apply_entry_button        -> findApplyEntryButton below
//   - find_job_posting_title_and_company -> scanJobPostingMetadata below
//
// Fill order matches this session's server-side fix: fields are filled
// FIRST, CAPTCHA is checked LAST, right before reporting the page done — a
// CAPTCHA widget on a real ATS form typically gates submission, not the
// fields themselves.

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

  return { jobUrl: window.location.href, company: metadata.company, title: metadata.title, fields };
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
      default:
        sendResponse({ error: `Unknown message type: ${message.type}` });
    }
  })();
  return true;
});
