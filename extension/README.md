# Autogram — Apply from Job Link (browser extension)

A second, complementary way to run Autogram's auto-apply workflow: instead of
the server launching its own Playwright-controlled browser, this extension
fills the form **inside your own already-logged-in Chrome tab**. Useful
specifically for login-gated sites (Naukri, LinkedIn, etc.) where there's no
session for a server-side browser to reuse.

It reuses the exact same backend as the web app — same profile, same résumé,
same `ApplicationAnswerEngine` (answer memory + LLM), same `applications`/
`application_questions` tables, same audit log. The only new backend surface
is `POST /automation/map-fields` and `POST /applications/{id}/report-status`
(see `app/api/automation.py`, `app/api/applications.py`).

**v1 is copilot-only: this extension never clicks Submit.** It fills what it
can, waits out any CAPTCHA it finds (never solves or bypasses one — see
`content-script.js`'s `pageHasCaptcha`/`findHumanGate`), and then hands off to
the existing web dashboard for review and the actual submit click.

**It never runs in Incognito.** No `"incognito"` key in `manifest.json`, and
`background.js`'s `getSafeActiveTab()` refuses outright if the current window
is Incognito — job sites that require login rely on your real, already-signed-in
session, which an Incognito window never has.

## Load it

1. Make sure the Autogram backend (`uvicorn app.main:app --reload`, default
   `http://127.0.0.1:8000`) and frontend (`npm run dev` in `frontend/`,
   default `http://localhost:5173`) are both running.
2. Open `chrome://extensions`, turn on **Developer mode** (top right).
3. Click **Load unpacked**, and select this `extension/` folder.
4. Click the extension's icon in the toolbar, sign in with your Autogram
   account (same one you use on the web app), and open a real job posting.
5. Click **Fill This Application**.

If your backend/frontend run somewhere other than the defaults, click the
gear icon in the popup (or right-click the extension → Options) and set the
correct URLs first.

## What it can't do (a platform limitation, not a design choice)

A content script cannot programmatically set a `<input type="file">`'s
value — browsers forbid it for security. So résumé upload is the one field
you still attach by hand; the popup tells you when a file field was found on
the page. Everything else — text, textarea, select, radio, checkbox — is
filled automatically from your profile, your answer history, or one LLM call
for genuinely novel questions, exactly like the server-side flow.

## Architecture notes

- `background.js` is the only thing that ever calls the backend — a service
  worker's `fetch()` isn't subject to page-origin CORS the way a content
  script's would be, so routing every API call through it sidesteps CORS
  entirely without needing to add the extension's origin to the backend's
  `CORS_ORIGINS`. The JWT lives only here (`chrome.storage.local`), never in
  page-context JS.
- `content-script.js` is the only thing that ever touches the DOM. It mirrors
  (in plain JS) three heuristics from `automation/browser/selectors.py` —
  `CAPTCHA_HINTS`/`_HUMAN_GATES`, `find_apply_entry_button`, and
  `find_job_posting_title_and_company` — kept intentionally close to the
  Python originals so a future change there is easy to notice needs mirroring
  here too.
- Fill order matches the server-side fix from this session: fields first,
  CAPTCHA check last, right before reporting the page done.
- No second review/audit UI: the popup's "Open in Dashboard" link goes
  straight to the existing web app's Application Detail page
  (`{frontendUrl}/applications/{id}`), which already has Answer Review, the
  pre-submission summary, and the full activity log — all of it works
  identically for extension-sourced applications with zero new frontend code.

## Known v1 limitations

- Copilot only — no extension-side auto-submit yet (a deliberate, documented
  follow-up, not an oversight).
- If a job posting's Apply button opens a **new tab** rather than navigating
  in place, this version doesn't follow it automatically (the server-side
  "Apply from Job Link" flow does, via Playwright's `context.expect_page()`;
  doing the same from a content script requires a background-script-driven
  tab-switch this v1 doesn't implement yet). The popup will say so — click
  the real Apply button yourself, then run "Fill This Application" again on
  the resulting page.
- No custom toolbar icon yet — Chrome shows its default extension icon.
