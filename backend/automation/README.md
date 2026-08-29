# `automation/` — browser automation module

Everything related to detecting an ATS platform, driving Playwright, mapping
form fields, and generating answers lives here. See
**[`../ARCHITECTURE.md`](../ARCHITECTURE.md)** for the full system design and
roadmap, and **[`interfaces.py`](./interfaces.py)** for the concrete
integration points into `app/`.

## Architecture: internal domain module, not a separate service

`automation/` is an **internal domain module of this same FastAPI
application** — the same process, the same deployment, the same database.
It is not independently deployable and is not designed to be. It shares the
application's infrastructure and is expected to use it directly:

- **Database**: real SQLAlchemy sessions from `app.core.database`, real ORM
  models from `app.models.db_models` (`CandidateProfile`, `ProfileDocument`, ...).
- **Repositories**: `app.services.profile_repository`, `app.services.document_storage`
  — automation reuses these instead of re-implementing profile/document access.
- **AI**: `app.ai.llm.router` (the existing `LLMRouter`, same retries/provider
  routing every other feature uses) and `app.services.embedding_service`
  (same embedding model/vector space as job matching).
- **Auth**: `app.core.auth.get_current_user` — automation-triggering routes
  authenticate the same way every other route does.
- **Config**: `app.core.config` — no separate settings/env story.

Automation extends the existing backend's capabilities; it does not
duplicate resume parsing, storage, matching, or LLM plumbing that already
exists in `app/`.

### Request flow

```
User
 |
FastAPI API                     (app/api/*, e.g. app/api/applications.py — Phase 4)
 |
Application Service Layer       (app/services/*, e.g. profile_repository, document_storage)
 |
Automation Module               (automation/agents, automation/applications)
 |
Playwright / ATS adapters / AI agents   (automation/browser, automation/ats, automation/forms, automation/agents)
 |
Existing Database + AI + Storage services   (app.core.database, app.ai.llm.router, app.services.embedding_service — called directly by automation/, not duplicated)
```

`app/api/*` routes stay thin (orchestration only, same as today); they call
into `app/services/*` and, starting Phase 4, into `automation/` (e.g.
`app/api/applications.py` calling `automation.agents.job_application_agent.JobApplicationAgent.start(...)`).
Automation code calls back down into the existing database/AI/storage layer
directly via `automation/interfaces.py` (or, once Phase 2+ work actually
starts, directly against `app.services.*` / `app.models.*` / `app.ai.*` —
`interfaces.py` is a convenience seam, not a hard requirement to route
through).

## What's still worth keeping separate (even in one monolith)

Sharing infrastructure isn't the same as no structure. These practices still
apply:

- **No circular imports.** `app.api.*` calls into `automation/`; `automation/`
  must never import `app.api.*` back. `app.services`, `app.models`,
  `app.core`, `app.ai` are fine to import from `automation/` in either
  direction of "who calls whom" at runtime — just not `app.api`.
- **No business logic in routes.** `app/api/applications.py` (Phase 4) stays
  a thin orchestrator: parse the request, call `automation/`, persist the
  result — same discipline `app/api/profile.py` and `app/api/resumes.py`
  already follow.
- **Use services/repositories, not raw queries, where they exist.**
  Automation code reads/writes profile and document data through
  `app.services.profile_repository` / `app.services.document_storage`
  (via `automation/interfaces.py`), not hand-rolled SQL.
- **ATS adapters stay isolated from each other.** Each platform
  (`automation/ats/<name>/`) is still its own module implementing the same
  `ATSAdapter` contract (`automation/ats/base.py`) — adding a new ATS still
  means adding one folder, not touching existing ones.
- **Browser automation stays isolated from database logic.**
  `automation/browser/*` (Playwright lifecycle, sessions, selectors) doesn't
  run ORM queries itself; it calls the functions in `automation/interfaces.py`
  when it needs profile/document data, keeping Playwright-specific code and
  data-access code in separate files.

The goal is an **integrated monolith** — one deployable application with
clear internal seams — not a microservice split.

## Layout

```
automation/
  interfaces.py       # integration points into app/ (read this first)
  browser/            # Phase 2 — Playwright lifecycle, sessions, selectors
  ats/                 # Phase 3/4/7 — ATSAdapter contract, ATSDetector, per-platform adapters
  forms/               # Phase 5/6 — FieldMapper, ApplicationAnswerEngine
  applications/        # Phase 4 — ApplicationFlowManager (multi-step navigation) — implemented
  agents/              # Phase 6 — LangGraph orchestration
  workers/             # Phase 4+ — Celery/ARQ queue consumer
  tests/                # unit tests (now run as part of the same app/ test environment)
```

`browser/browser_manager.py`, `browser/session.py`, `browser/selectors.py`,
and `ats/detector.py` are implemented (Phase 2/3) — session persistence,
retries, screenshots, traces, generic Next/Submit/file-upload/CAPTCHA DOM
helpers, and ATS platform detection (URL-pattern + DOM-fingerprint tiers) are
all real. `ats/base.py` (the adapter contract) is real and now also carries
shared fill helpers (`_fill_first_match`, `_fill_known_questions`,
`_match_label_to_profile_attribute`, reusing `forms/field_mapper.py`'s
`FIELD_SYNONYMS` table) that concrete adapters build on. `ats/greenhouse/greenhouse_adapter.py`
and `ats/lever/lever_adapter.py` are implemented (Phase 4) — real field
selectors, resume upload, label-matched question answering, and submit —
tested against realistic HTML fixtures in `automation/tests/test_greenhouse_adapter.py`
/ `test_lever_adapter.py`. `ats/workday/workday_adapter.py` is implemented
too — see "Long, multi-page applications" below for why it couldn't be
registered before that support existed. `ats/registry.py` maps a detected ATS
platform name to its real adapter class (`greenhouse`/`lever`/`workday`
today) — callers check `get_adapter_class(...) is None` and route to
`needs_review` instead of invoking a still-stub adapter (SmartRecruiters,
Taleo, iCIMS, Ashby, BambooHR, Oracle HCM). `applications/application_flow_manager.py`
is also implemented: `ApplicationFlowManager` drives one application
end-to-end through any `ATSAdapter` — CAPTCHA/human-gate checks on EVERY page,
résumé upload on whichever page asks for it, a verified page-by-page
navigation loop (see below) capped at `MAX_PAGES`, the auto-submit/needs-review/
copilot-review decision (`decide_action()`, mirroring ARCHITECTURE.md's
decision table exactly), and a screenshot/trace/error-log captured for every
run — tested end-to-end against a real headless browser and `data:` URLs in
`automation/tests/test_application_flow_manager.py`.

## Long, multi-page applications

A Greenhouse posting is one page. A Workday application is 4-6 — My
Information, My Experience, Application Questions, Voluntary Disclosures,
Self Identify, Review — each an SPA transition rather than a page load. The
original step loop assumed the first shape and broke on the second in three
separate ways, all reproduced on a real 5-page fixture before the fix:

1. **No wait after clicking Next.** `click()` returns the instant the click
   lands, not when the next step has rendered — so the following fill pass ran
   against the PREVIOUS page's DOM.
2. **No proof the click did anything.** A validation-blocked click looked
   identical to a successful one, so a rejected page was refilled and
   re-clicked until the step cap ran out — then treated as the final page. A
   5-page application was scored, and could be handed over, on the basis of
   page 1.
3. **The résumé uploaded exactly once, before the loop.** Useless on a form
   whose upload field lives on page 2, as Workday's does.

**The fix is `automation/applications/page_navigator.py`.** It defines a
`PageSignature` — a page's URL, title, heading, its own "Step 2 of 5"-style
progress text, and the IDENTITIES (never values) of its visible controls — and
`advance_to_next_page()`, which clicks, waits for the page to settle, and
compares signatures before and after to decide whether the form genuinely
moved. Two properties this is built to guarantee:

- **Filling a field must never look like navigation.** The signature excludes
  every field's value and the page's body text on purpose — both change the
  moment a field is typed into, and a check that fired on that would report
  every page as "advanced" without the run ever leaving page 1.
- **A revealed conditional field must never be mistaken for a page turn.**
  `PageSignature.newly_visible_controls()` is what tells the fill loop
  ("Do you require sponsorship?" → "Which visa do you hold?") to run another
  fill round on the SAME page rather than navigating past a field that just
  appeared.

`ApplicationFlowManager._process_page()` is the resulting per-page cycle: settle
and dismiss cookie/consent overlays (`browser/selectors.py::dismiss_overlays`,
`wait_for_overlays_to_clear`) → check CAPTCHA/human-gates (now on EVERY page,
not just the first) → upload the résumé if THIS page asks for one
(`_upload_resume_if_offered`, keyed on `resume_attachment_state() == "missing"`,
not on which page number this happens to be) → fill in rounds until nothing new
is revealed (`MAX_FILL_ROUNDS`) → run the vision fallback over whatever is still
empty HERE, not only on the final page → ask the adapter whether this is the
last page. If not, `_advance_to_next_page()` navigates with proof and retries
at most once, and only after something demonstrably changed (an overlay
dismissed, a validation-flagged field filled) — never twice against an
identical page, which is the endless-retry loop the old attempt cap was a
band-aid for. Navigation that can't be proven ends the run as
`manual_required` with the form's own validation errors attached, rather than
silently continuing against a page the application never left.

**Nothing in the loop is ATS-specific.** Which control advances the form,
whether this is the last page, and what this page is called are all
`ATSAdapter` methods (`find_next_control`, `find_submit_control`,
`is_final_page`, `is_review_page`, `page_label`, `dismiss_distractions`) with
working generic defaults built on `browser/selectors.py` — so a platform
overrides only what it needs to.

**A subtle correctness fix that fell out of this work:** the generic
label/name-or-placeholder sweeps in `ats/base.py` used to read every label on
the page regardless of which step it belonged to. On a wizard that keeps every
step in the DOM and toggles `display` (Workday's own accordions, and most
hand-rolled multi-step forms), that meant a field belonging to a LATER page was
both scored as a failure on this one (diluting confidence with fields that
were never on screen — 0.17 vs. the correct 1.0 on the integration fixture) and
marked "examined" — permanently, since that marker is never cleared — making it
unfillable once the form actually reached it. Both sweeps now skip a matched
field that isn't currently *visible*, without marking it, so it is judged
exactly once: on the page where the user can actually see it.

**Workday** (`ats/workday/workday_adapter.py`) is the concrete adapter this
work targets. Its fields are found by `data-automation-id` rather than by
label text or CSS class — ids from Workday's own shared component library,
stable across tenants and redesigns. Its single quirk worth an override: ONE
button (`bottom-navigation-next-button`) drives the whole application, reading
"Next"/"Save and Continue" throughout and "Submit" on the review page — so
`is_final_page()` reads that button's own label rather than asking "is there a
Next button?", which would say "keep going" forever on the review page.
Workday requires an account (this app never creates one — see "No password
harvesting" below) and is deliberately absent from `PUBLIC_ATS_PLATFORMS`, so
`decide_action()` can never return `AUTO_SUBMIT` for it: a completed Workday
application is always handed to a human to submit, confirmed at 1.0 confidence
in the integration run.

Tested in `automation/tests/test_page_navigator.py` (the signature/navigation
primitives, against real rendered pages), `automation/tests/test_workday_adapter.py`,
and the multi-page end-to-end tests in `test_application_flow_manager.py` —
which drive `data:` URLs whose inline JS genuinely turns the page (heading
changes, fields swap, the button relabels to "Submit"), 1/2/3/5 pages, so what
they exercise is real page-turning behavior, not a simulation of it.

**Manual review handoff.** `needs_review`/`copilot_review`/`manual_required`
all mean "a human needs to look at the actual page" — but the browser used
to close automatically the instant a run finished, regardless of outcome,
leaving nothing to actually look at. `ApplicationFlowManager` now accepts a
`headless` override; when a caller passes `headless=False` (which
`app/api/applications.py` does whenever `autopilot_enabled=False` — copilot
mode) AND the run lands on one of `REVIEW_STATUSES`, the browser is left
open instead of closed (`should_keep_browser_open()`). There's no
auto-cleanup: each such run spawns its own visible Chromium window that
stays open until someone closes it by hand. Autopilot runs stay headless
(unattended) as before, and every existing headless test/caller is
unaffected since the default is still whatever `AUTOMATION_HEADLESS` says.

Because `run()` executes inside a FastAPI `BackgroundTasks` call, "not
calling `.close()`" isn't by itself enough to guarantee the browser survives:
once the background task function returns, every local variable (the
`ApplicationFlowManager`, its `BrowserManager`, the Playwright driver
connection) goes out of scope, so without something else holding a real
Python reference, that connection is fair game for garbage collection —
which could tear down the "kept open" browser anyway, silently. A
module-level `_OPEN_REVIEW_SESSIONS` registry in
`application_flow_manager.py` holds that reference for as long as a review
session is left open; `close_review_session(application_id)` closes one and
forgets it, and `list_open_review_sessions()` lists what's currently open.

Phase 4 also added the `app/` side of the handoff: `app/models/db_models.py`'s
`Application`/`AutomationRun` tables, `app/services/application_repository.py`,
and `app/api/applications.py` (`POST /applications/start`, `GET /applications`,
`GET /applications/{id}`, `GET /applications/{id}/runs`). There's no
Celery/Redis queue yet, so `POST /applications/start` hands off to a FastAPI
`BackgroundTasks` job in the same process — it detects the ATS
(`automation.ats.detector.detect_ats_for_url`), resolves the adapter via
`automation.ats.registry`, runs `ApplicationFlowManager`, and persists the
`ApplicationRunResult` it gets back. Every other concrete adapter under
`ats/<platform>/` (Workday, SmartRecruiters, Taleo, iCIMS, Ashby, BambooHR,
Oracle HCM, generic) is still a stub — that's Phase 7.

`forms/field_mapper.py`'s `FieldMapper` is also implemented (Phase 5):
`FieldMapper.map_field(label=..., placeholder=..., name=..., nearby_text=...)`
resolves one field's raw DOM signals to a `(profile_attribute, confidence)`
pair, checked in order of decreasing certainty — `name`/`id` attribute (with
camelCase/snake_case/kebab-case/bracket-notation normalization) first, then
label text, then placeholder, then nearby text — and returns `None` rather
than guess when nothing matches (see the module docstring for the
deliberate "ambiguous short field name" trade-off this implies).
`ATSAdapter._fill_known_questions()` (Phase 4) now delegates to it in two
passes: every `<label>` on the page first, then — for anything not already
examined — every remaining input/select/textarea's `name`/`placeholder`
directly, which is what actually lets a field rendered with no `<label>` at
all get filled. A shared `data-automation-examined` DOM marker (set by
`_fill_first_match` and the label pass alike) stops the second pass from
redundantly re-filling — and re-counting toward `ApplicationFlowManager`'s
confidence score — a field another path already handled.

`forms/answer_engine.py`'s `ApplicationAnswerEngine` is also implemented
(Phase 6) — it answers whatever `FieldMapper` couldn't. By the time a
question reaches it, `FieldMapper` has already tried and failed to match its
label/name/placeholder, so what's left is either a real fact phrased in a
way `FieldMapper`'s substring synonyms didn't cover, or a genuinely
subjective/novel question ("Why do you want to work here?"). Two paths,
cheapest first:

- **Deterministic** (free, never fabricated): a small keyword classifier
  recognizes a handful of recurring factual shapes — work
  authorization/sponsorship, notice period, salary expectation, years of
  experience — and answers straight from `CandidateProfile`. If the profile
  field is empty, it declines (same "never guess" philosophy as
  `FieldMapper`) rather than answer, and falls through to the LLM path
  instead — inventing a candidate's salary or start date would be actively
  harmful, not just wrong.
- **LLM** (costed, one batched `app.ai.llm.router` call per form via
  `answer_batch()`, not one call per question): every question that isn't a
  recognized factual shape, plus any factual shape whose profile field was
  empty. The prompt explicitly instructs the model never to invent concrete
  personal facts it wasn't given; a failed/malformed/empty response leaves
  the question unanswered rather than typing a placeholder/error string
  into a real application field — which correctly pulls down
  `ApplicationFlowManager`'s confidence score for that run instead of
  silently pretending the question was handled.

Both paths are backed by a persistent, per-user, exact-match answer cache
(`app/services/answer_cache_repository.py`, the `answer_cache` table) keyed
by a hash of the *normalized* question text — a screening question repeated
on a later application (extremely common across postings on the same ATS
family) costs nothing the second time, whether it was originally answered
deterministically or by the LLM.

Wiring: `ATSAdapter.__init__` takes an optional `answer_engine=` (default
`None`, preserving exact pre-Phase-6 behavior — every adapter/test built
before Phase 6 is unaffected); `ApplicationFlowManager` threads it straight
through into whatever adapter a run constructs; `app/api/applications.py`
builds one per run from the candidate's profile, this run's DB session (for
the cache), and the optional `job_description` hint on `POST
/applications/start`'s request body.

**Scope note — the LangGraph agent layer is deferred.** ARCHITECTURE.md's
Phase 6 roadmap cell originally bundled `ApplicationAnswerEngine` together
with `automation/agents/*.py` (`JobApplicationAgent`/`ProfileAgent`/
`AnswerAgent`, LangGraph-based orchestration). Only the answer engine
shipped this round — `agents/*.py` are still `NotImplementedError` stubs.
`app/api/applications.py::_run_application` already performs the
orchestration `JobApplicationAgent` was meant to wrap (ATS detection ->
adapter resolution -> `ApplicationFlowManager.run()` -> persistence), so
introducing a LangGraph state graph on top is a separate architectural
decision — a new dependency and a new orchestration paradigm for
error-recovery/retries — better scoped as its own follow-up pass than
folded in alongside the answer engine.

## `forms/field_handlers.py` — the Field Handler Registry

Everything above this line answers "which profile attribute does this field
mean" (`FieldMapper`) or "what should the answer text be" (`ApplicationAnswerEngine`).
`field_handlers.py` answers a different, previously-unaddressed question:
**how do I actually interact with, and confirm success on, this specific DOM
widget** — and it sits directly between "field detected" and "confidence
calculated" in every adapter's fill path
(`ApplicationFlowManager → ATSAdapter → field detection → field filling
(field_handlers.py) → confidence calculation → review/auto-submit`).

Before this existed, each of `ATSAdapter`'s four fill call-sites
(`_fill_first_match`, and the fill branches of `_fill_questions_by_label`,
`_fill_questions_via_answer_engine`, `_fill_questions_by_name_or_placeholder`)
had its own small inline "if it's a `<select>` call `select_option`, otherwise
`.fill()`" branch, none of them verified the value actually stuck, and
non-native widgets (react-select comboboxes, searchable/virtualized
dropdowns, the real country picker, file uploads) weren't handled at all —
country fields and "how did you hear about us"-style dropdowns silently
failed, and resumes were never actually attached.

**Core abstractions:**

- `Field` — a small introspected wrapper around a Playwright `Locator`
  (`tag_name`, `input_type`, `role`, plus the `label` and `profile_attribute`
  the caller already knows) built by `describe_field()` via one batched
  `evaluate()` call.
- `FieldHandler` (ABC) — `supports(field)`, `fill(field, value)`,
  `verify(field, value)`. Ten concrete handlers implement it:
  `TextInputHandler`, `TextAreaHandler`, `NativeSelectHandler`,
  `ReactSelectHandler`, `ComboboxHandler`, `CountryPickerHandler`,
  `CheckboxHandler`, `RadioHandler`, `DateHandler`, `FileUploadHandler`.
  `ReactSelectHandler`, `ComboboxHandler`, and `CountryPickerHandler` share one
  real implementation (`_DropdownHandler`) rather than duplicating
  open/search/scroll/select logic three times — they differ only in
  `supports()` (which widget shape they claim) and, for the country picker,
  an alias-aware match (`USA`/`US`/`America` → "united states", etc.).
- `FieldHandlerRegistry` — an ordered list of handlers; `resolve(field)`
  returns the first one whose `supports(field)` is true. `DEFAULT_HANDLER_REGISTRY`
  orders them most-specific-first (file upload, checkbox, radio, date, native
  select, country picker, react-select, generic combobox, textarea, text
  input last as the catch-all).
- `fill_field(field, value)` — the single orchestration entry point every
  adapter fill path now calls: resolves a handler, fills, verifies, retries up
  to `DEFAULT_MAX_ATTEMPTS` (3) times on verification failure, and returns a
  `HandlerOutcome(filled, actual_value, failure)`. On failure, `failure` is a
  structured `FieldFailure(field_label, field_type, expected_value,
  actual_value, failure_reason, retry_count)` — `failure_reason` is either
  `"no_handler_matched"` (registry couldn't classify the widget at all) or
  `"verification_failed"` (a handler was found and tried but the value never
  stuck after retries) — instead of a generic "could not fill field" string.
  Every step logs (field detected, handler selected, dropdown opened,
  searching/scrolling for an option, option selected, verification
  passed/failed, retry N, resume uploaded, country selected, unknown field
  type), per the requirement that every field produce traceable log lines.

**Widget-detection heuristics** (no hardcoded per-company selectors anywhere):
ARIA roles/attributes (`role="combobox"/"listbox"/"option"`,
`aria-haspopup`, `aria-expanded`, `aria-autocomplete="list"`) for generic
comboboxes; class-name conventions (`react-select__control`,
`css-<hash>-control`) or an id containing `react-select` for react-select
specifically; `field.profile_attribute == "country"` (set by `FieldMapper`)
plus "not a native `<select>`" for the country picker; native `<select>` is
checked first so it never gets mis-routed into the dropdown handlers at all.
Virtualized/scrollable dropdowns (only a handful of options rendered at a
time) are handled by re-querying visible options fresh after each scroll of
the dropdown's own container — never the page — rather than caching a
snapshot, so DOM-node-recycling lists are searched correctly.
`FileUploadHandler` never checks visibility (Greenhouse/Lever's real
`<input type=file>` is deliberately hidden behind an "Attach" button) — it
uses `set_input_files()` directly and verifies via the input's `files`
property, falling back to a visible-text search for the filename for ATS UIs
that hide the real input entirely; `ATSAdapter.upload_resume()` clicks a
visible "Attach"-style trigger first if no file input is present yet, then
delegates to this handler.

**Explicit scope boundaries — not addressed this round:**

- `ATSAdapter.upload_resume()` keeps its existing public `bool` return
  contract (used by `ApplicationFlowManager`); the richer `FieldFailure` detail
  from the underlying handler is logged (`logger.warning`) but not threaded
  further up through `ApplicationRunResult`. Widening that return type is a
  separate, larger change (touches `ApplicationFlowManager` and its tests) and
  wasn't part of this request.
- `FieldFillResult.failure` is now populated end-to-end, but
  `ApplicationFlowManager`'s confidence-aggregation formula itself is
  unchanged — it still only looks at `FieldFillResult.confidence`/`filled`.
  Using `failure.failure_reason` to weight confidence differently (e.g.
  penalizing `verification_failed` more than a field that was simply left
  blank) is a natural follow-up once there's real run data to tune it against,
  not a change made speculatively here.
- Lever has no dedicated new field-handler test file — the fill mechanism is
  100% shared via `ats/base.py`, so it's already exercised through
  `test_greenhouse_adapter.py` and the standalone
  `test_field_handlers.py` suite; adding an identical Lever-flavored copy
  would test the same code path twice.

Tested in `automation/tests/test_field_handlers.py` (registry resolution,
`fill_field()` orchestration including the retry-to-structured-failure path,
and each handler individually — including hand-built HTML fixtures for a fake
react-select, a no-search-input country listbox, and a virtualized-lite
scrollable listbox) plus one end-to-end integration test in
`test_greenhouse_adapter.py` (`answer_questions()`'s label sweep resolving a
react-select-style country field all the way through `CountryPickerHandler`).
As with the rest of this session's work, none of it has been executed in this
environment (no shell/Playwright runner available here) — it's written from
documented DOM/ARIA patterns and traced by hand; running it against the real
test suite, and then against a couple of real Greenhouse/Lever postings, is
the natural next step before trusting it in production.

`forms/answer_engine.py`, `agents/`, and `workers/` are still interface
stubs (`NotImplementedError`) — see the roadmap in `ARCHITECTURE.md` for
build order. `automation/tests/` has its own
`conftest.py` (mirroring `tests/conftest.py`) since `automation/browser/session.py`
and `automation/interfaces.py` import real `app.core.config`-backed modules
and need the same env vars (`DATABASE_URL`, `ENCRYPTION_KEY`, etc.) — this
lets `automation/tests/` run standalone or alongside `tests/` either way.

## Phase 8 — compliance profile fields, question classification, widget hardening

Builds on everything above without redesigning any of it — no handler
removed, no engine rewritten, Playwright unchanged.

**Compliance/EEO data (`app/models/db_models.py`, `app/services/profile_repository.py`,
`app/api/profile.py`).** `CandidateProfile` gained four columns:
`work_authorized`/`requires_sponsorship` (booleans — genuinely different
facts; a candidate can be authorized today AND still need sponsorship
later), `visa_type` (free text), `sponsorship_countries` (JSONB list). A
new, SEPARATE `candidate_demographics` table (gender, veteran_status,
disability_status, race_ethnicity) holds voluntary EEO answers — never
folded into `candidate_profiles` and never written by anything except the
user's own explicit `PUT /profile/demographics` call. `GET /profile/demographics`
returns `None` per field when the candidate has never been asked, which is
how the automation layer knows to prompt once rather than re-derive an
answer.

**Question classification (`automation/forms/question_classifier.py`).**
A narrower replacement for `answer_engine.py`'s old single `work_authorization`
bucket: `classify_question()` maps a full screening-question sentence to
one of `requires_sponsorship` / `work_authorized` / `visa_type` /
`notice_period_days` / `expected_salary` / `years_of_experience` / four
`demographic_*` categories, or `None` (never guesses). `ApplicationAnswerEngine`
uses it to pick the right `CandidateProfile` field and formatter — falling
back to the old free-text `work_authorization`/`visa_status` echo for any
profile that hasn't set the new booleans yet, so this is fully backward
compatible. **Demographic categories are a hard exception**: they are
answered ONLY from `candidate_demographics`, NEVER by the LLM and NEVER
inferred — if nothing is stored yet, `answer_batch()` returns
`AnswerResult(source="needs_user_input")` for that question and it never
enters the batched LLM call at all, regardless of how many other
subjective questions are in the same batch.

**Widget-interaction hardening (`automation/forms/field_handlers.py`).**
Two new handlers: `ToggleHandler` (`role="switch"`/`aria-checked` — clicks
only when the current state doesn't already match the target, since a
switch toggles rather than sets) and `VirtualizedListboxHandler` (an
already-expanded `role="listbox"` panel — country/location/years pickers
and custom questions rendered as a long, lazily-rendered list with no
click-to-open step at all, as opposed to `_DropdownHandler`'s
trigger-then-popup-elsewhere pattern; registered ahead of `ComboboxHandler`,
behind `CountryPickerHandler`). `CheckboxHandler` now claims `role="checkbox"`
in addition to `<input type=checkbox>`, and tries native `check()`/`uncheck()`
first (1s timeout — see `_FAST_ACTION_TIMEOUT_MS`, so a genuinely hidden
input fails fast) then its associated `<label>`, then the element itself,
then a wrapping container, then a JavaScript-only state flip as the
explicit last resort. `RadioHandler` now resolves THREE group shapes — a
native `<input type=radio>` group (by shared `name`, as before), an ARIA
`role="radio"` group (by a `role="radiogroup"` ancestor or shared parent),
and a plain `<button aria-pressed>` choice group (by shared parent) —
verifying via `is_checked()`/`aria-checked`/`aria-pressed`, whichever
applies, with a keyboard (focus + Space) fallback for widgets that only
listen for keyboard interaction. A bare `<button>` with no `aria-pressed`
is never claimed — a submit/"Next" button must never be routed through
`RadioHandler`.

**Shared utilities (`automation/utils/` — new package: `element_actions.py`,
`scrolling.py`, `retry.py`).** The scroll-a-dropdown-container and
poll-for-a-popup logic that used to live as private helpers inside
`field_handlers.py` is now `scroll_container_until_option_found()` and
`wait_for_dynamic_element()`, usable by any handler; `safe_click()`
centralizes scroll-into-view + click with a short, fail-fast timeout and an
optional fallback locator. `retry.py`'s `retry_strategies()` is a small
generic "try each of these named strategies in order, stop at the first
success" helper for any future handler that wants PART 12's "Attempt 1
normal, Attempt 2 wait+retry, Attempt 3 alternate selector, Attempt 4
keyboard, Attempt 5 JS fallback" shape without hand-rolling it. All three
modules are pure Playwright + stdlib, same as `field_handlers.py` itself.

**Structured failure reporting (PART 13).** `FieldFailure` gained
`widget_type` (the resolved handler's class name, or `"unknown"`) and
`context` (an arbitrary dict the caller supplies — `automation/ats/base.py`'s
new `ATSAdapter._fill_context()` passes `{"ats_type": self.name, "url": self.page.url}`
into every `fill_field()` call it makes) — both additive with safe
defaults, so every pre-Phase-8 `FieldFailure(...)` construction site still
works unchanged. `format_failure_report()` renders the "FIELD AUTOMATION
FAILURE" block (field, detected widget, expected/actual value, attempt
count, failure reason, plus whatever context was supplied) and is logged
automatically by `fill_field()` on both failure paths (`no_handler_matched`
and `verification_failed`).

**Explicit scope boundaries — not addressed this round** (documented here
per this codebase's own convention, not silently skipped): `company` isn't
threaded into `_fill_context()` — neither `ATSAdapter` nor
`ApplicationFlowManager` currently holds it (the `Application` DB row does,
but the automation layer never reads back its own tracking row); adding it
is a small follow-up once there's a real call site that has it in hand.
Button-based choice-group DISCOVERY (resolving a `<label>`/question to a
`<button>` locator in the first place) isn't wired into
`ats/base.py::_input_for_label`'s generic sweep — `RadioHandler` can
correctly fill/verify a button-group once handed one, but nothing in the
generic label/name sweep resolves to a `<button>` yet; concrete ATS
adapters that render screening questions as button groups will need their
own discovery selectors. Real ATS validation (PART 15 — Greenhouse, Lever,
Workday) has not been run against live postings in this environment (no
Playwright runner available here, same caveat as every prior phase's
README note) — that's the natural next step before trusting any of this in
production.

### Phase 8 production-readiness audit (post-implementation review)

A follow-up audit of everything above, against a specific reliability
checklist — findings below, with the safe/additive fixes actually applied
(no handler rewritten, no control flow restructured beyond what's noted).

**1. Handler selection determinism.** Confirmed deterministic: `FieldHandlerRegistry.get_handler()`
always returns the first handler (by `DEFAULT_HANDLER_REGISTRY` list order)
whose `supports()` is true — same handler, every call, for the same field
shape. Multiple handlers CAN legitimately match the same field (e.g. a bare
`role="listbox"` element matches both `VirtualizedListboxHandler` and
`ComboboxHandler`; a country field rendered as a listbox matches
`CountryPickerHandler` too) — this was previously true but invisible. Added
`get_all_matches()` (every matching handler, in order) and had
`get_handler()` log a debug line whenever more than one handler matches,
naming all of them and which one won. This is a lightweight resolver in
the sense the request asked for — it makes the existing priority-order
resolution observable — without introducing a second selection mechanism
alongside `FieldHandler`/`FieldHandlerRegistry`.

**2. fill() → verify() pairing.** Confirmed: `fill_field()`'s loop always
calls `verify()` immediately after `fill()`, every attempt, unconditionally
— there is no path where a fill is presumed successful without a live DOM
read back. Two integrity gaps found and fixed:

- `_read_dropdown_displayed_value()`'s fallback used `text_content()`,
  which includes hidden descendant text. If a widget's popup/menu is
  nested INSIDE the resolved control (rather than a sibling, as this
  module's own fixtures and react-select's own convention both are), an
  unselected option's text sitting in a still-closed, hidden menu could
  satisfy the loose substring match and report a false "filled" success
  with nothing actually selected. Fixed by switching that one fallback to
  `inner_text()`, which only returns rendered/visible text — regression
  test: `test_dropdown_verify_does_not_false_positive_on_hidden_descendant_text`
  in `tests/test_phase8_audit_fixes.py`.
- Several state reads (`is_checked()` in `CheckboxHandler`/`ToggleHandler`/
  `RadioHandler`, the dropdown option's `click()`, the native checkbox/radio
  `check()`/`uncheck()`) had no explicit timeout, meaning a genuinely
  unreachable element would silently fall back to Playwright's 30-second
  default — on the FIRST attempt of what's supposed to be a fast-fail
  step in a multi-strategy fallback chain, and on every `verify()` call.
  Fixed with a short explicit timeout (`_FAST_ACTION_TIMEOUT_MS` = 1s for
  state checks/native actions, `safe_click`'s own 3s default for clicks).

**3. VirtualizedListboxHandler.** Confirmed: always-mounted listboxes,
dynamically/lazily rendered options, and internal (container-only) scroll
all work as designed — re-verified with a 60–120-item fixture per test
file. Confirmed no `page.mouse.wheel()` anywhere in `automation/` (grepped).
One real gap found: `supports()` claimed ANY `role="listbox"` element
regardless of visibility, which would incorrectly steal a click-to-open
combobox's popup (resolved directly, before ever being opened) away from
`ComboboxHandler` — it would then correctly-but-uselessly fail (every
option reads as not-visible) instead of ever being opened. Fixed by adding
an `is_visible()` guard to `supports()` — a hidden `role="listbox"` now
falls through to `ComboboxHandler`, which knows how to open it first.

**4. CountryPickerHandler.** Country selector and searchable dropdown: already
covered by existing tests. Virtualized dropdown + alias matching TOGETHER
was previously untested (each was tested separately) — added
`test_country_picker_handles_alias_matching_inside_a_virtualized_dropdown`
in `tests/test_ats_pattern_fixtures.py`, which also had to correct its own
scroll-distance math to fit inside the handler's real, unmodified
`max_scroll_attempts=12` rather than tuning the handler to fit the test.
Phone country-code selector: a REAL, DOCUMENTED GAP, not silently claimed
as handled — `FieldMapper` has no synonym mapping a phone-country-code
label to the `country` profile attribute (correctly so: a candidate's
profile country and their phone's calling-code prefix are not always the
same fact), so such a field never gets `profile_attribute == "country"`
and never reaches `CountryPickerHandler`'s alias matching at all; it falls
to whichever generic handler matches its DOM shape with plain substring
matching only. Solving this properly needs a product decision (a new
"phone calling code" profile concept, or a deliberate derivation rule from
`country`) — out of scope for an audit pass, and now has an explicit
regression test (`test_phone_country_code_selector_is_not_routed_through_country_picker`)
documenting the current, honest behavior instead of leaving it as an
unstated assumption.

**5. Failure reporting.** Had: field label, expected/actual value, widget
type, ATS type + URL (via `ATSAdapter._fill_context()`). Missing: the
actual exception text (only a generic `failure_reason` string existed) and
any element snapshot. Added `FieldFailure.last_exception` (the last
`PlaywrightError` message across every attempt — `None` when every attempt
completed without raising, which is itself informative: a pure value
mismatch, not a crash) and `FieldFailure.element_html` (a best-effort,
500-char-capped `outerHTML` snapshot of the failed element). Both additive
with safe defaults — no existing `FieldFailure(...)` construction site
needed to change. Per-field SCREENSHOTS were deliberately NOT added: a form
with many failed fields taking a full-page screenshot per field is
disproportionate, and `ApplicationFlowManager` already captures one
run-level screenshot on `failed`/`needs_review`/`copilot_review` outcomes
(`_safe_screenshot`) — the element HTML snapshot is the proportionate
per-field complement to that existing run-level screenshot, not a
replacement for it.

**6. Realistic ATS integration fixtures.** Added
`automation/tests/test_ats_pattern_fixtures.py`: a Greenhouse-style
Rails-array-named radio group, a Lever-style custom dropdown trigger +
options list, and a Workday-style `data-automation-id`-tagged async/
virtualized combobox (Workday has no dedicated adapter yet — this exercises
the generic `ComboboxHandler` fallback's readiness for that markup shape).
Where a fixture assumes ARIA hints a real platform's markup might not
actually expose (noted inline in each fixture), that's flagged as an
assumption to confirm against the live posting, not asserted as fact.

All of the above is covered by `automation/tests/test_phase8_audit_fixes.py`
(items 1–5) and `automation/tests/test_ats_pattern_fixtures.py` (item 6, plus
the CountryPickerHandler virtualized+alias case from item 4) — like every
other test in this module, not executed in this environment (no Playwright
runner available here); traced by hand against the real implementation.

## Option-aware answering

`ApplicationAnswerEngine` used to receive questions as bare strings, which
meant it was answering fixed-choice controls blind: it could only produce
prose, and nothing stopped it proposing a choice the form doesn't offer.
Prose is useless for a `<select>`, and an invented choice is worse than
useless — `field_handlers` would try to select a value the DOM has no option
for and report a `FieldFailure`.

**What flows now.** `ATSAdapter._fill_questions_via_answer_engine()` describes
each pending control once, reads its real choices via
`field_handlers.read_field_options()`, and passes them as
`answer_engine.Question(text, options)`. The engine then:

- appends `_OPTION_PROMPT` to its system prompt (only when the batch actually
  contains options — a prose-only form produces byte-for-byte the prompt it
  did before), instructing the model to answer with one listed option
  verbatim, matched on MEANING rather than wording;
- re-resolves whatever comes back against the real option list in
  `_match_option` and replaces it with the verbatim option string. **Prompting
  is not the mechanism — this is.** A model that ignores the instruction
  cannot get an invented choice onto the page;
- declines (empty answer, `0.0` confidence) when the answer resolves to
  nothing, or to two options equally — the existing
  `ANSWER_REVIEW_CONFIDENCE_THRESHOLD` path then leaves the field for a human
  rather than filling a near-miss.

`_match_option` is three tiers, tightest first: exact equality, then
case/whitespace-normalized equality, then containment **only when exactly one
option matches** (so `"Yes"` resolves against `["Yes, now or in the future",
"No"]`, but not against `["Yes, I am authorized", "Yes, with sponsorship"]`).
Ambiguity is never broken by picking the first or longest candidate — same
"never guess between plausible matches" rule `FieldMapper` follows.

Deterministic answers get snapped to the form's own wording too
(`requires_sponsorship=False` + `["YES", "NO"]` → `"NO"`). A profile fact that
*can't* be mapped mechanically (`"US Citizen"` against `["Yes", "No"]`) falls
through to the LLM, which can read it semantically — that widens what gets
answered without widening what can be typed, since the LLM's answer is
option-validated on the way back out. **Demographic answers are the exception
that never falls through**: an unmappable stored demographic goes to a human,
because inferring one is exactly what that path exists to prevent.

`AnswerResult.available_options` echoes the list back on every result,
including declined ones, so a review UI can show a human the real choices
instead of making them re-read the form.

**How each widget's options get read.** Native `<select>` reads from
`el.options`; radio-flavored groups (native radio, `role="radio"`, button
choices) reuse `RadioHandler`'s own group resolution, so the engine sees
exactly the options the handler will later select among. Neither touches the
page.

Custom dropdowns (react-select, ARIA combobox/listbox) are **probed**:
`_probe_dropdown_options()` clicks the trigger, waits for the popup, reads the
options, and presses Escape. It never clicks an option, so it cannot change
the field's value.

That probe was deliberately left out of the first version of this work, on the
principle that a pre-answer read should be side-effect-free — and that was
wrong in a way worth recording. On a real Greenhouse posting
(`job-boards.greenhouse.io`), *every* screening dropdown is exactly this kind
of widget, so refusing to open them meant the engine answered all of them
blind and the feature didn't reach the case it was built for. Clicking a
trigger open and pressing Escape is what a human applicant does before
deciding, and `_DropdownHandler` re-opens the widget from scratch at fill time
regardless. Two honest limits remain: a **searchable** dropdown that fetches
options as you type shows only its initial results here, and a **virtualized**
list only materializes its visible window — so for a long list (countries,
universities) these options are a sample, not the full set. That's fine for
the screening questions this exists for, and is why `_match_option` treats
"not in the list" as a reason to ask a human rather than proof the answer is
wrong.

**Cache.** Keyed by question TEXT alone, so the same question can return with
a different option set. A cached answer is re-resolved against the current
form's options and a hit that resolves to nothing is treated as a **miss** —
one extra LLM call is cheaper than handing the page a value it has no option
for.

## Answers, not commentary

Observed on a live Greenhouse posting: the salary field received *"The
candidate profile does not specify CTC expectations."* and the preferred-name
field *"My preferred name is not specified in the profile; please use the
candidate's..."*. Both were typed into the employer's form and read by a human
as the candidate's own words.

Two causes, both now fixed:

1. **`_SYSTEM_PROMPT` asked for it.** It said to "answer honestly and
   generically instead of making one up" when a fact was missing — so the
   model wrote an honest, generic sentence about the fact being missing. It
   now says the opposite: if you don't have what the question asks for, return
   `null` and let a human fill it in. It also now distinguishes value
   questions from prose questions — "Navikenz", not "My most recent employer
   is Navikenz" (the other thing that posting got wrong).
2. **The confidence gate couldn't catch it.** The model rated that answer
   above `ANSWER_REVIEW_CONFIDENCE_THRESHOLD` (0.80), because as a *sentence*
   it was accurate. Self-reported confidence can't police form-of-answer, so
   `_META_COMMENTARY_PATTERNS` does it in code — same "the prompt asks, the
   code decides" split as `_match_option`. A match is discarded outright, at
   `0.0` confidence, before option matching even runs.

The pattern list is deliberately conservative — each entry is phrasing that
could only ever be commentary addressed to the operator. Note `"the candidate
profile"` is listed but plain `"the candidate"` is not: *"the ideal candidate
for this role..."* is a legitimate opening for a real free-text answer.

`Question` is a `str` subclass rather than a dataclass, deliberately: a
question has been a bare string for this class's whole existence, and every
ATS adapter test double duck-types `answer_batch(list[str])`. Being a real
string means all of that keeps working untouched while option-aware code
reads `.options` off the same object.

Covered by `automation/tests/test_answer_engine_options.py` (26 tests, most of
which simulate a model *ignoring* the instructions — the whole point is that
the DOM's option list and the pattern check, not the model's word, decide what
may be typed) and `automation/tests/test_read_field_options.py` (10 tests
against real rendered widgets, including a Greenhouse-shaped dropdown whose
options only exist once opened, and assertions that probing leaves it closed
and unset).

Unlike the Phase 8 work above, these were executed. Pre-existing failures
elsewhere in the suite are unrelated to this change and reproduce identically
at `HEAD`.

## Three bugs only a real form exposed

Validated against the live posting at `job-boards.greenhouse.io` (read the
options, fill the dropdowns; never submitted). Option-reading was necessary
but nowhere near sufficient — the fill path was broken on that markup in three
independent ways, and every existing fixture was too tidy to show any of them.
The shared pattern: **helpers that search the whole `page` where they should
search the field's own widget.** Fine with one dropdown on the page, wrong with
nine.

**1. A decoy option container.** `_LISTBOX_SELECTOR`'s `div[class*='dropdown' i]`
branch matches intl-tel-input's phone-country wrapper (`div.iti--inline-dropdown`),
which sits on every Greenhouse application, is permanently visible, and holds
244 country options — all hidden. `_find_listbox_container()` returns the FIRST
visible match, so both the probe and `_find_matching_option()` inspected that
decoy, found no visible options, and concluded the dropdown was empty; the real
`select__menu-list` was two nodes further down the same query. Fixed with
`_option_containers()`, which returns every visible container that actually
holds visible options, and callers that scan all of them.

**2. Page-global search-input lookup.** Every react-select question is an
`input[role='combobox'][aria-autocomplete='list']`, so that form has nine
`_SEARCH_INPUT_SELECTOR` matches, all visible. `_DropdownHandler.fill()` called
`_find_search_input(page)`, got the first one, and typed every answer into
question 1. Fixed with `_field_search_input(field)` — the field itself, then
inside it, then its immediate shell — before any page-wide fallback.

**3. The value isn't on the element the label points at.** `<label for=...>`
resolves to react-select's `input`, which has no children, is set to
`opacity: 0` once a value is chosen, and has its `value` cleared by the
library. All three lookups in `_read_dropdown_displayed_value()` therefore came
back empty on a dropdown that had just been filled *correctly*: `verify()`
failed, `fill_field()` burned all three attempts re-selecting an
already-selected option, and reported `verification_failed`. The chosen text is
one hop up, in a sibling `select__single-value`. Fixed with
`_display_value_scopes()`, which also checks the widget's `control`/`container`
ancestor.

That third fix is deliberately narrow: ancestor scopes are used ONLY with the
explicit `_DISPLAY_VALUE_SELECTOR`, never with a broad `inner_text()`. On this
very form `select__container` includes the question's own label, and
`_values_match` is substring-tolerant in both directions — so reading a
container's whole text would let the label *"...If not, are you willing to
relocate..."* satisfy a check for the value `"No"`. There's a regression test
for exactly that.

Fixing (1) and (3) also fixed a pre-existing suite failure,
`test_ats_pattern_fixtures.py::test_lever_style_dropdown_selects_referral`,
which had been failing at `HEAD`.

Pinned by `automation/tests/test_dropdown_live_form_shapes.py` — two
react-select widgets plus the phone-widget decoy, rather than the single tidy
dropdown the other fixtures use. The four fill/verify tests in it were
confirmed to FAIL at `HEAD` and pass after the fix.

**Live result.** All three screening dropdowns on that posting now read
`('Yes', 'No')`, fill with the intended value, and verify — `filled: True`,
`actual='Yes'`/`'No'`, no retries. The probe leaves each widget closed and
unset.

## Unlabeled questions (Lever) + a dangerous synonym match

Found by running the sweep against a live Lever posting
(`jobs.lever.co/AIFund/.../apply`) whose four required screening questions came
out completely empty.

**They were structurally unreachable, not mis-filled.** That page has ten
`<label>` elements and every one belongs to a standard field (resume, name,
email, phone, location, company, URLs). The screening questions have no
`<label>`, no `aria-label`, no `aria-labelledby`, and no `id` a label could
point at — the question text is a plain sibling `<div class="text">`:

```html
<li class="application-question custom-question">
  <div>Are you legally authorized to work in the United States?✱</div>
  <div class="application-field required-field">
    <input type="text" name="cards[<uuid>][field2]" placeholder="Type your response" required>
  </div>
</li>
```

So `_fill_questions_by_label` was blind to them and
`_fill_questions_by_name_or_placeholder` had only `cards[<uuid>][field2]` and
`"Type your response"` to work with, neither of which matches anything.
`_fill_questions_by_nearby_text` (pass 1b) recovers the text by walking up a
few ancestors and taking the first whose text reads like a question **while
that ancestor still contains exactly one control** — which is what stops it at
the `<ul>` instead of scooping up the whole form. Recovered questions join the
SAME batched answer-engine call as the labeled ones, so this costs no extra LLM
call. Matches are scored `NEARBY_TEXT_MATCH_CONFIDENCE` (0.55), deliberately
below the auto-submit bar: proximity-recovered text is a weaker signal than a
real label.

Two things about that page worth keeping in mind: there are **five** screening
inputs, not the four that are visible, and Lever ships a hidden
`cards[<uuid>][baseTemplate]` decoy input whose ancestor chain jumps straight to
the whole form — the single-control guard is what rejects it.

**Pass 1b runs before pass 2, which needed an explicit precedence guard.**
`FieldMapper`'s tiers are name/id (0.97) > label (0.9) > placeholder (0.75) >
nearby text (0.55), but 1b has to run early to make the single batched engine
call. Without a guard that ordering silently outranks a stronger signal: a bare
`<input name="linkedin_url">` with no label would be claimed by 1b on proximity
text, marked examined, and queued for the engine — and with no engine injected
the pending list is discarded, leaving the field EMPTY when pass 2 would have
filled it deterministically. 1b now checks `map_field(name=, placeholder=)`
first and leaves any such field completely untouched.

**The synonym bug this exposed is the more serious finding.** `FieldMapper`
matched synonyms by plain substring containment, so:

```
"Are you legally authorized to work in the United States?"  ->  ('state', 0.9)
```

`"state"` sits inside `"States"`. At 0.9 — above the 0.85 auto-submit bar — the
candidate's home state would have been typed into a work-authorization question
and submitted. Matching is now anchored to word boundaries
(`_synonym_pattern`), which fixes it without loosening the "simple and
explainable" contract or changing the deliberate `name="company"` behaviour:

```
"...authorized to work in the United States?"  ->  ('work_authorization', 0.9)
"...require visa sponsorship...United States?" ->  ('requires_sponsorship', 0.9)
"State" / "State/Province"                     ->  ('state', 0.9)   # unchanged
```

`"province"` in `"provincial"`, `"city"` in `"capacity"`, and `"currency"` in
`"concurrency"` were the same failure waiting to happen.

**Status.** The synonym fix is verified (21/21 mapper tests) and the text
recovery is confirmed correct at the DOM level — each real input yields exactly
its own question. The end-to-end pairing on the live form is NOT yet confirmed:
a run mis-paired two questions with their inputs, and that harness passed
`profile=None`, so whether the fault is the code or the stub is still open. Not
claimed as working until that's separated.

## Human-paced input

`automation/utils/human_input.py`, called from `field_handlers`, so all ten
handlers and all ten adapters inherit it rather than each fill path
re-implementing it: scroll into view → settle 500–1200ms → click → settle
200–500ms → clear → type character by character at 30–90ms → pause 2s before
the next field.

**The reason is correctness first, realism second.** Playwright's `fill()` sets
the value and dispatches one `input` event. Plenty of ATS fields accept that,
but a control listening for real keystrokes — an autocomplete that filters as
you type, a react-select search box, a masked phone input, a character counter
gating the Submit button — sees a single bulk mutation and either ignores it or
lands in an inconsistent state. `press_sequentially()` produces a genuine
`keydown`/`keypress`/`input`/`keyup` stream per character, which is what those
widgets are written against. The secondary benefit is that filling thirty
fields in under a second is a traffic pattern no applicant produces, and some
boards throttle on it.

Three deliberate limits:

- **Per-chunk, not per-character, delays.** `press_sequentially` takes ONE
  delay for the whole string, so a literal per-character random delay would
  mean one round-trip per character — ~500 for a cover-letter answer, costing
  far more in IPC than it buys. Typing in 8-character chunks with a freshly
  rolled delay each time gives varying cadence at a fraction of the overhead.
- **`MAX_TYPED_CHARS` (400) falls back to `fill()`.** A 2,000-character cover
  letter at 60ms/char is two minutes of typing for a `<textarea>` with no
  keystroke-sensitive behaviour — the correctness argument simply doesn't apply
  to long-form prose, and the cost is real.
- **`input[type=date]` is never typed.** It renders as segmented day/month/year
  spinners whose keystroke handling follows the browser locale, so typing
  `1990-05-14` lands the parts in a different order per locale. `fill()` sets
  the unambiguous ISO value the element actually stores.

The field must also be cleared before typing (`press_sequentially` appends —
otherwise re-filling a pre-populated field yields `"AdaAda"`), and every step
degrades to a plain `fill()` on error, since `fill_field()`'s verify step is
what ultimately decides success.

Controlled by `AUTOMATION_HUMAN_PACING` (**on by default**; any of
`0`/`false`/`no`/`off` disables it). `automation/tests/conftest.py` sets it to
`0` — the suite fills hundreds of fields and 2s apiece alone would add over an
hour. `automation/tests/test_human_input.py` re-enables it per-test and asserts
on the recorded `keydown`/`input` event stream rather than wall-clock timing,
which would be flaky.

**Live result.** The same three dropdowns fill and verify correctly with pacing
ON, at roughly 7s per field including the option probe and the inter-field
pause. Budget ~2–3s per field: a 30-field form goes from seconds to about two
minutes.

## Stored-answer coverage — the fields a live Lever form left blank

Source: `jobs.lever.co/leverdemo-8/a41e218e-01c6-4334-9849-dff3e0c027f6/apply`.
Every text field, dropdown and radio group on that form filled correctly. Five
things did not: **Pronouns**, **Gender** (EEO select), **Race**, **Veteran
status**, the **"I identify my ethnicity as"** checkbox group, and the
**"can contact me about future job opportunities"** opt-in. Three unrelated
causes, fixed independently.

**1. Stored demographic tokens never matched the form's wording**
(`automation/forms/demographic_matching.py`). `candidate_demographics` stores
canonical tokens — `non_binary`, `decline_to_answer`, `not_veteran`,
`no_disability` — and real ATS dropdowns word the same answers as prose
("Non-binary", "Decline to self-identify", "I am not a protected veteran", "No,
I do not have a disability and have not had one in the past"). `match_option`
correctly refuses every one of those pairings, so `_demographic_answer` found a
stored value, failed to map it, and surfaced the question for a human anyway —
the exact outcome storing the answer was supposed to prevent. The fix is a
token→phrasing layer in front of the matcher (mechanical variants first, then a
small ordered table of real-world phrasings, longest first so a spelled-out
option wins over a bare "No" that containment would find ambiguous), NOT a
looser matcher: every candidate string still has to resolve to exactly one
option the DOM really has, and an unresolvable value still goes to a human.
`match_option` itself moved to `automation/forms/option_matching.py` unchanged,
because a second caller now needs the identical rules.

**2. Checkbox GROUPS were unreachable by every pass**
(`ats/base.py::_fill_checkbox_groups`). Pass 1 sees each member's own `<label>`
("He/him"), which is an *option*, not a question, and matches no `FieldMapper`
synonym; `_collect_for_answer_engine` then drops it (`_NON_FILLABLE_INPUT_TYPES`
excludes checkboxes); pass 3's selector excludes them; and
`_fill_consent_checkboxes` only looks at required legal text. The new pass walks
up from each checkbox to the innermost ancestor that holds 2+ checkboxes and has
prose of its own — the tightest container, so an EEO section holding both a
pronoun group and an ethnicity group is never merged into one question — then
answers it through `ApplicationAnswerEngine.stored_choices()`, which **never
calls the LLM for any question**. Stricter than `answer_batch` deliberately: a
wrongly-ticked box is indistinguishable from a deliberately-ticked one on a
review screen, and the groups this exists for are pronouns and ethnicity.
Members of a group nothing could answer are left UNMARKED, so two adjacent
required consent boxes (technically a two-member "group") still reach
`_fill_consent_checkboxes` instead of silently blocking submission.

**3. Nothing could act on the marketing opt-in**
(`ats/base.py::_fill_opt_in_checkboxes`). `CandidateProfile.marketing_opt_in` is
tri-state and only an explicit `True` ticks the box; `None` (never asked) and
`False` both leave the page exactly as it rendered. It reuses
`field_mapper.looks_like_opt_in_label` — one definition of "this label is a
marketing opt-in", shared by the code that refuses to treat it as a profile
value and the code that acts on a real yes.

**New profile fields.** `CandidateProfile`: `highest_education_level` (free text
in the form's own vocabulary — "Bachelor's Degree" — because every ATS words its
education dropdown differently), `willing_to_relocate`, `marketing_opt_in`.
`CandidateDemographics`: `pronouns` (free text, not an enum: one live form offers
nine sets plus "Custom") and `ethnicities` (JSONB list, for "select all that
apply", falling back to `race_ethnicity` when only that is set).

**New question categories.** `demographic_pronouns` — a demographic category,
checked *before* `demographic_gender` since forms label it "Gender pronouns", and
the one field where an LLM answer would be worst: the only thing a model could
infer pronouns from is the candidate's name. Plus `highest_education_level` and
`willing_to_relocate`, which are ordinary profile-backed factual categories: when
the column is empty they fall through to the LLM+résumé path exactly as before,
so the columns make the common case free and deterministic without taking away
the fallback that already answered them.

**What still goes to the LLM.** Everything else, unchanged — with the résumé
facts and the two new columns now in the prompt payload. `marketing_opt_in` is
deliberately kept OUT of the prompt: it is a consent decision, not a fact about
the candidate, and the only thing allowed to act on it is the opt-in pass above.

### Second tier — seven more profile fields, and one wrong answer

Same exercise as above, applied to what a form asks *after* the EEO block. Six
gaps and one defect.

**The defect first, because it was a wrong answer rather than a blank.**
`question_classifier` listed `"current ctc"` among the EXPECTED-salary phrases,
so "What is your current CTC?" resolved to `expected_salary` and the candidate's
*target* number was typed into a field asking what they earn today. A blank field
costs a few seconds of review; a number asserting the candidate already earns
their target is a negotiating position they never chose. `current_salary` /
`current_salary_currency` are now their own columns, category, formatter and
`FieldMapper` entry — the same "two different facts, never inferred from each
other" split as `work_authorized` vs `requires_sponsorship`.

**The gaps.** `preferred_name` ("What should we call you?" — falls back to
`first_name`, which is not a guess), `referral_source` ("How did you hear about
this job?"), `employment_type_preference` (stored as a token, reshaped to
"Full-Time" because the raw token matches neither "Full-time" nor "Full Time"),
`languages`, and `willing_background_check` (tri-state, like `marketing_opt_in`).

**Language fluency is the one category that can't be an attribute formatter.**
The question names *which* language — "Are you fluent in German?" — so the answer
depends on the question text, not the profile alone, and
`answer_engine._language_fluency_answer` handles it in the same question-aware
way the demographic path does. A live Lever posting asked exactly this as a
required radio group (Yes / No / Limited Working Proficiency) and it came back
blank. `languages` is therefore a list of `{language, proficiency}` rather than
bare names: answering a fluency question from a bare mention of English would be
an assumption about degree. Three deliberate refusals, each of which would
otherwise be an inference rather than an answer — a language the candidate never
listed (an absent entry means "they didn't mention it", not "they don't speak
it"), a question naming two of their languages at once, and an entry with no
proficiency recorded. A `conversational` speaker gets "Limited Working
Proficiency" where the form offers that band, and "No" where it only offers
yes/no; `FLUENT_LANGUAGE_PROFICIENCIES` draws the line where the candidate drew
it and is never widened for a better fill rate.

**Two ordering constraints worth not breaking.** `preferred_name` sits *before*
`first_name` in `FIELD_SYNONYMS` because "preferred first name" contains "first
name" — the other order fills the preferred-name box with the legal first name
and leaves the real one empty. And `languages` has no bare `"languages"` synonym:
"Programming languages" is a near-universal field on engineering applications,
and filling it with spoken languages would be confidently wrong.

## A résumé that uploaded, verified, and then wasn't there

A real run against a live Greenhouse posting (`job-boards.greenhouse.io`)
finished with `resume_uploaded` in its checkpoints, `resume_upload succeeded` in
its log, and no résumé on the application. The final screenshot shows the
Resume/CV section still offering "Attach / Dropbox / Google Drive / Enter
manually" — nothing attached — under a form that was otherwise filled correctly.

The Playwright trace says exactly what happened, to the millisecond:

| t | event |
|---|---|
| 4.27s | `set_input_files` on `#resume` succeeds |
| 4.27s | verification passes — `el.files.length == 1`, `files[0].name` is the résumé |
| 4.89s | `Minified React error #418` — hydration error |
| 4.92s | `React recovered from an error during hydration` (×4, errors #425/#418/#423) |
| 6.35s | the same `#resume` input reads back **empty**; the Attach buttons are back |

`page.goto(..., wait_until="domcontentloaded")` returns when the server's HTML
is parsed, which on a Remix/React ATS is *before* the app hydrates. The upload
went into the pre-hydration DOM; React recovered from a hydration error by
re-creating that part of the tree; the file went with the element that was
discarded. Verification never caught it because verification ran 600ms **before**
the DOM was thrown away — it was correct at the moment it was taken and
worthless by the end of the run.

Two changes, and the second is the one that actually guarantees the outcome:

1. **`selectors.wait_for_form_ready(page)`** after navigation — a bounded,
   never-raising wait for `load` then `networkidle`, so filling starts on a
   hydrated form rather than racing it.
2. **`ATSAdapter.ensure_resume_attached()`**, called by
   `ApplicationFlowManager` at the *end* of the run. It re-reads the live page
   (`resume_attachment_state()` → `attached` / `missing` / `no_field`) and
   re-uploads if the form dropped the file. `no_field` is deliberately distinct
   from `missing`: a later step of a multi-step form isn't a page that lost the
   résumé, and conflating the two would make every such step re-attempt an
   upload it has no field for. If the résumé still won't stick, the run's
   résumé result flips to unfilled — a run that would submit without a résumé
   has to go to a human, and reporting it as uploaded is the one outcome worse
   than failing.

`resume_attachment_state()` checks the input's own `files` first and *then*
whether the upload widget's text contains the filename, because some ATS UIs
upload straight to S3 and clear the input — reading `files` alone would call
those "missing" and re-upload on every check.

Waiting for hydration alone would not have been enough, which is why the
end-of-run re-check exists: hydration recovery can fire at any point, and a
verification is only ever a statement about the moment it was taken.

## Vision fallback — the fields no amount of DOM reading can answer

Every pass before this one reads the DOM. `FieldMapper` matches a label to a
profile field; `ApplicationAnswerEngine` answers from the profile or from one
batched text LLM call. Both are limited to the text the ATS *exposes*, and on
the same live Greenhouse run three kinds of field survived them — all of them
obvious to anyone looking at the page:

- **A conditional follow-up.** `If yes to the above question, what role and what
  governmental organization?` — required, and preceded by a "government
  official?" question answered **No**. Read on its own the field is
  unanswerable, so the text engine correctly declined rather than invent an
  employer and a role. A person types `N/A` without thinking about it.
- **A control whose visible value isn't its own value.** `candidate-location`
  and `country` were reported as unfilled required fields while the form plainly
  showed "Noida, Uttar Pradesh, India" and "+91" — react-select and country
  pickers keep the selection outside the input whose value the scan reads.
- **A field whose label the DOM never connects to it**, so no pass ever knew
  what it was asking.

`forms/vision_fallback.py` runs last, over only the required fields still empty,
and sends what a person would look at: a **cropped screenshot per field** —
generously padded upward (`_VISION_CROP_PADDING`, 240px) so the question above
and its answer are in frame, which is the entire point for a conditional
follow-up — plus the same candidate payload the text engine uses
(`ApplicationAnswerEngine.profile_payload()`, shared rather than re-derived, so
the two passes can never disagree about what the profile says). One batched
vision call, then answers are filled through the ordinary `fill_field()`
pipeline, so a vision answer is verified against the live DOM exactly like any
other.

It is a fallback, never a shortcut: it is the most expensive path in the system
(one high-detail image per field, capped at `MAX_FIELDS_PER_CALL`, anything over
the cap named in the log rather than silently dropped), and anything it answers
*repeatedly* is a signal the cheap deterministic path is missing a case. Off via
`AUTOMATION_VISION_FALLBACK=false`.

The guardrails are the text path's guardrails, because the output goes to the
same place — an employer's form, in the candidate's name:

- **Demographic/EEO questions are dropped before the call**, not answered. A
  screenshot doesn't change who may answer a question about gender, race,
  disability, veteran status, or pronouns; only the candidate can, via
  `PUT /profile/demographics`.
- **Options are re-resolved against the DOM's real option list**
  (`option_matching.match_option`) and an unmatched answer is discarded, so the
  model cannot invent a choice — the prompt asks, the code decides.
- **Meta-commentary is discarded** ("the candidate profile does not specify...").
- **`already_filled` is respected, not overwritten.** A field the screenshot
  shows as answered is left alone and reported back separately
  (`VisionPassOutcome.confirmed_already_filled`); the flow manager then stops
  counting it as a missing required field, logging each waiver by name. That is
  the narrow, evidence-based fix for the react-select false positives above —
  without it every such form is permanently `manual_required` over values that
  are demonstrably already there.
- **Answers are gated by the same `ANSWER_REVIEW_CONFIDENCE_THRESHOLD` (0.80)**
  as generated text answers, and an entry with no usable confidence is treated
  as unanswered rather than trusted.
- **Response entries are matched by the model's own `field` number**, not by
  position: a reordered or partial response must not shift answers onto the
  wrong questions, which is the one failure mode here that types a real answer
  into the wrong employer's field.

The crops sent to the model are saved to `logs/<application_id>/vision-field-N.png`
— the first thing worth looking at when a vision answer seems wrong.

## Running in the user's own Chrome — `AUTOMATION_BROWSER_MODE`

Every run used to start with `chromium.launch()` + `browser.new_context()`. That
pair is a brand-new browser with a brand-new, **incognito-equivalent** profile:
no cookies, no logged-in LinkedIn/Gmail/Workday/Greenhouse session, and a second
window competing with the browser the user is actually sitting in front of. On
any ATS behind a login that meant hitting an account wall (`find_human_gate` →
`manual_required`) on runs that would have sailed through in the user's own
browser, where they're already signed in.

`BrowserManager` now selects between three ways of getting a context
(`AUTOMATION_BROWSER_MODE`, implemented in `browser/chrome_attach.py`):

| mode | what you get | closes the browser? |
| --- | --- | --- |
| `cdp` **(default)** | Playwright `connect_over_cdp()` to the user's **already-running** Chrome; the job opens as a **new tab** in their existing window, on their real profile | never — only the tabs we opened |
| `persistent` | our own normal (**not** incognito) window on a real, reusable profile directory; cookies survive between runs | yes, it's ours |
| `launch` | the original throwaway browser + empty context seeded from the encrypted `SessionStore` — for CI and headless servers | yes, it's ours |

Three details in `cdp` mode carry the whole feature:

- **`browser.contexts[0]`, never `browser.new_context()`.** Over CDP,
  `new_context()` creates a fresh *incognito* context **inside** the user's
  Chrome — a separate cookie jar with none of their logins, i.e. the exact
  problem we set out to fix. The default context is the only one wired to the
  on-disk profile.
- **We don't own that browser.** `close()` closes the tabs this run opened and
  drops the driver connection. `browser.close()` is never called on an attached
  browser: over CDP it only disconnects today, but it is one API change away
  from taking a human's whole browser down, so it isn't in that path at all.
- **An attached browser reports `headless=False`.** That's what
  `should_keep_browser_open()` reads to decide whether there's anything for a
  human to review, so a `copilot_review` run leaves its tab on screen instead of
  closing the one thing the human was supposed to look at.

### Starting Chrome so we can attach to it

Chrome only speaks CDP if it was started with the flag, and — this is the part
that surprises everyone — **a second `chrome.exe` aimed at a profile that is
already open just forwards its command line to the running instance and exits,
so the port never opens.** To attach to the Chrome holding your real logins,
close Chrome completely first, then start it yourself:

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
```

Use it as your normal browser from then on; every Autogram run opens a tab in it.
If nothing is listening on the port, `AUTOMATION_CDP_AUTOLAUNCH` (default on)
starts Chrome with the port open on a dedicated profile under
`storage/chrome_profile/cdp/<user_id>/` and attaches to that. That Chrome is
started **detached** and deliberately left running, so the second and every
later application land as new tabs in the same browser — you log into an ATS
once, by hand, and it stays logged in. Set
`AUTOMATION_CHROME_USER_DATA_DIR=chrome-default` to autolaunch against your real
Chrome profile instead (only works while Chrome is fully closed, for the reason
above).

`--user-data-dir` **must be absolute.** A relative path is resolved against
Chrome's own working directory, and when Chrome can't use it, it falls back to a
default profile *without opening the debug port* — a silent failure that looks
like a 30-second timeout next to a perfectly healthy Chrome window. Our default
(`storage/chrome_profile`) is relative, so `launch_chrome_with_remote_debugging`
resolves it; this cost a real debugging session to find.

### Limitations of CDP, and what happens instead

- **The port has to be open before we can attach.** Nothing can retrofit remote
  debugging onto an already-running Chrome — no API, no injection. Hence the
  autolaunch-and-reuse path above.
- **Enterprise policy can forbid it** (`RemoteDebuggingAllowed=false`), as can a
  port already taken by something else.
- **Tracing may be unavailable** on a context we attached to rather than
  launched, because Playwright doesn't control that browser's launch arguments.
  `start_trace()` returns `False` instead of raising, and the run continues
  without a `trace.zip` — screenshots and the error log are unaffected.
- **We can't apply `SessionStore` storage-state to an attached context**, and
  don't try: `save_session()` is a no-op in `cdp`/`persistent` mode. Chrome's own
  profile persists cookies better than we can, and exporting `storage_state()`
  from an attached context would copy the user's *entire* cookie jar — every
  site, not just this ATS — into our storage. Not needed, and not ours to take.
- **`ats/detector.py::detect_ats_for_url` still opens its own throwaway headless
  browser**, but only for URLs that tier-1 pattern matching can't classify, and
  it closes it immediately. Inside the apply flow the detector is normally given
  the already-open page instead.

Anything that stops `cdp` from working falls through to `persistent` — a normal
window on a persistent profile. Note what it never falls back to: `launch`.
Silently downgrading to an empty incognito context would strip away every login
the user expects to still have, which presents as "the ATS logged me out again"
rather than as the misconfiguration it is.

Selection, ownership and cleanup are covered by
`automation/tests/test_browser_attach.py`, which fakes Playwright — including
the two assertions that matter most: no `new_context()` inside the user's
Chrome, and no `browser.close()` on a browser that isn't ours.
