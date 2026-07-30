# Universal ATS Job Application Automation Platform — Architecture

This document extends the existing **AI Job Application Agent** backend (resume parsing,
matching, tailoring — see `README.md`/`PROJECT_REPORT.md`) into a full **auto-apply**
platform. It covers system architecture, database schema, folder structure, and the
build roadmap. Phases 1–5 (Master Candidate Profile, browser automation
foundation, ATS detection, the Greenhouse/Lever adapters + application flow
orchestration + `applications` tracking API, and `FieldMapper`'s dynamic
field resolution) are implemented. Phase 6's `ApplicationAnswerEngine`
(deterministic + LLM screening-question answers, backed by a persistent
per-user answer cache) is implemented too; the LangGraph agent layer
(`automation/agents/*.py`) that was bundled into the same roadmap cell is
deliberately deferred — see the Phase 6 roadmap note. Phase 7 remains
scaffolded as interfaces only.

> **Architecture decision on file (per product owner sign-off):** the browser is driven
> **server-side by Playwright**, not by a browser extension. The backend owns persistent,
> per-user, per-ATS authenticated browser contexts (cookies/local-storage saved to
> encrypted storage after a one-time manual login) so it can operate against
> login-gated ATS platforms (Workday, Taleo, iCIMS, Oracle HCM) as well as public ones
> (Greenhouse, Lever, SmartRecruiters, Ashby, BambooHR). This is a deliberate deviation
> from a lower-risk browser-extension design and carries real obligations — see
> **Compliance & Risk** below. This is not legal advice; get a technology lawyer's
> review before commercial launch.

---

## 1. System Architecture

**Two top-level modules, one narrow boundary between them:**

```
ai-job-agent/
  app/            existing FastAPI backend (untouched) + Phase 1 profile system
  automation/     everything browser/ATS/agent-related — a separate module
```

`app/` is the project you already had (auth, resumes, job matching/tailoring)
plus the Phase 1 profile system, which belongs in `app/` because it's plain
CRUD over Postgres, no different in kind from `resumes`/`jobs`. `automation/`
is new, physically separate code with **one rule: it never imports `app.*`**.
The only thing that crosses the boundary is `automation/interfaces.py` — see
**§1a** below. `automation/` lives in its own top-level folder for code
organization (see `MIGRATION.md`) but is an internal module of this same
FastAPI application, not an independently deployable one — see the updated
§1a below and `automation/README.md`.

Within `automation/`, four layers, each independently testable:

```
┌─────────────────────────────────────────────────────────────────────┐
│  app/  (FastAPI, unchanged + api/profile)                            │
│  api/auth · api/profile · api/applications (implemented, Phase 4)    │
└───────────────────────────────┬─────────────────────────────────────┘
                                 │  app.api calls into automation directly;
                                 │  automation.interfaces re-exports app.* +
                                 │  legacy compatibility types (see §1a)
┌───────────────────────────────▼─────────────────────────────────────┐
│  automation/  Orchestration Layer                                    │
│  agents/ (LangGraph: JobApplicationAgent, ProfileAgent, AnswerAgent) │
│  applications/application_flow_manager.py (multi-step navigation)   │
│  workers/ (Celery/ARQ tasks — one apply run = one queued job)       │
└───────┬───────────────────────────────────────────────┬─────────────┘
        │                                               │
┌───────▼───────────────────┐               ┌───────────▼───────────┐
│  automation/ats/           │               │  automation/browser/   │
│  base.py (interface)       │◄──uses────────┤  browser_manager.py    │
│  detector.py                │               │  session.py            │
│  greenhouse/…                │               │  selectors.py          │
│  lever/…                      │               │  (Playwright, per-user │
│  workday/…  (etc.)             │               │   persistent contexts) │
│  automation/forms/              │              └────────────────────────┘
│    field_mapper.py                │
│    answer_engine.py                 │
│    field_handlers.py (widget I/O)     │
└───────────────┬────────────────────┘
                │
┌───────────────▼──────────────────────────────────────────────────────┐
│  Data Layer — accessed via app.services/app.models (both app/ routes  │
│  and automation/ code call these directly; no separate DB layer)      │
│  PostgreSQL (SQLAlchemy + Alembic) · Redis (queue, Phase 4+) ·        │
│  S3-compatible storage (resumes/certs) · pgvector (reused for         │
│  question→answer-cache similarity search)                             │
└────────────────────────────────────────────────────────────────────────┘
```

### 1a. The `app` ↔ `automation` boundary (updated — see `automation/README.md`)

`automation/` was originally designed as a fully isolated package (no `app/`
imports at all, injected callables for everything). That decision was
reversed: `automation/` is now an **internal domain module of the same
FastAPI application** — same process, same deployment, same database — and
is expected to import `app.core.*` / `app.models.*` / `app.services.*` /
`app.ai.*` directly. `automation/interfaces.py` documents this contract in
two parts: real wrapper functions over `app/` (Section A — prefer these, or
the underlying `app/` modules directly, for new work) and the original plain
dataclasses/Protocols (Section B — kept only so still-stub Phase 6/7 files
keep importing successfully; `ApplicationRunResult` is the one Section B type
every phase actually uses as automation's real return value).

Concretely, as implemented in Phase 4:

- `app/api/applications.py` fetches the real `CandidateProfile` and
  `ProfileDocument` ORM rows itself (`app.services.profile_repository`) and
  passes them straight into `ApplicationFlowManager` — no intermediate view
  object, no injected callables.
- `automation/` returns a plain `ApplicationRunResult`; `app/` decides how to
  persist it — `app/services/application_repository.py::apply_run_result`
  writes to the `applications` / `automation_runs` tables.
- The one rule that didn't change: dependencies are one-directional,
  `app.api -> automation -> app.services/app.models/app.core/app.ai`.
  Nothing under `automation/` may import `app.api.*` (that would create the
  cycle `app.api.applications -> automation -> app.api.applications`).

`automation/tests/` still runs standalone or alongside `tests/` (its own
`conftest.py` bootstraps the same env vars `app/`'s tests need), but it is no
longer zero-setup/containerizable independently of `app/` — see
`automation/README.md` for the full rationale.

### Request lifecycle (single application) — as implemented (Phase 4)

1. `POST /applications/start {job_url, autopilot_enabled}` (`app/api/applications.py`) →
   idempotency check (`application_repository.get_by_user_and_url` — same `job_url_hash`
   the DB's `uq_applications_user_job_url` constraint enforces); if none exists, a row is
   created in `applications` (`status=pending`), the profile's default (or explicitly
   chosen) resume is picked, and a `BackgroundTasks` job is queued →
   `202 {application_id, status: "pending"}`.
2. Background task (`app/api/applications.py::_run_application`, still in the same
   FastAPI process — no Celery/Redis queue yet, that's Phase 4+): marks the row
   `processing`, then `automation.ats.detector.detect_ats_for_url(job_url)` detects the
   platform. `automation.ats.registry.get_adapter_class(ats_platform)` resolves the real
   adapter; if none exists yet for that platform (Phase 7), the row goes straight to
   `needs_review` with a clear reason instead of attempting automation.
3. `ApplicationFlowManager(...).run()` — its own `BrowserManager` launches/reuses a
   persistent Playwright context for `(user_id, ats_platform)`; a CAPTCHA on the page
   short-circuits to `manual_required` before anything is filled (human-in-the-loop, §9).
4. Otherwise the adapter is driven step by step: `upload_resume` → `fill_personal_information`
   → `answer_questions` → (repeat across multi-page flows, detecting "Next"/"Continue",
   capped at `MAX_STEPS`) → the auto-submit/needs-review/copilot-review decision →
   `submit_application` if auto-submitting. Every actual DOM interaction inside those
   steps — typing into an input, selecting a country, opening a searchable/virtualized
   dropdown, ticking a checkbox, uploading a resume file — is delegated to
   `forms/field_handlers.py` (below); adapters never contain widget-specific fill logic
   themselves.
5. `FieldMapper.map_field()` (Phase 5) resolves each field's `name`/`id` attribute, label
   text, placeholder, or nearby text to a profile attribute, tiered by decreasing certainty
   (name/id > label > placeholder > nearby text) — `ATSAdapter._fill_known_questions()`
   sweeps the page in two passes (every `<label>` first, then every remaining
   name/placeholder field with no `<label>` at all) using it. A labeled question
   FieldMapper can't resolve is handed to Phase 6's `ApplicationAnswerEngine` (if one was
   injected — see next step); with none injected, it's left unanswered, same as before
   Phase 6 existed.
6. **Phase 6.** `app/api/applications.py` builds one `ApplicationAnswerEngine` per run
   (candidate profile + this DB session, for its persistent per-user answer cache + the
   optional `job_description` hint from the request body) and passes it straight through
   `ApplicationFlowManager` into the adapter. It answers whatever FieldMapper couldn't:
   a small deterministic classifier recognizes recurring *factual* question shapes
   (work authorization/sponsorship, notice period, salary expectation, years of
   experience) and answers straight from the profile — never fabricated, and it declines
   (falls through to the LLM path) rather than guess if the relevant profile field is
   empty. Everything else — genuinely subjective/novel questions — is answered by ONE
   batched `app.ai.llm.router` call per form (`answer_batch()`), never one call per
   question. Every answer (deterministic or LLM) is cached per user, keyed by a hash of
   the normalized question text (`answer_cache` table, §2), so a repeated question across
   applications costs nothing the second time. A failed/empty LLM answer is never typed
   into the form — the field is simply left unfilled, which correctly pulls down that
   run's confidence score instead of silently pretending the question was handled.
7. Every step is checkpointed and logged; a screenshot, Playwright trace, and error log are
   captured for the run (§14, `automation_runs`).
8. `automation/` returns an `ApplicationRunResult`; `application_repository.apply_run_result`
   persists it — the `applications` row is updated (`applied`/`failed`/`manual_required`/
   `needs_review`/`copilot_review`) and a new `automation_runs` row gets the run's artifacts.
9. `GET /applications/{id}` reflects live status; `GET /applications/{id}/runs` returns the
   full run history (useful once retries exist). No websocket/event stream yet — poll.

### Compliance & Risk (server-side Playwright model)

Because the backend controls the browser directly (rather than the user's own logged-in
tab), the following are **hard requirements**, not suggestions:

- **No password harvesting.** For login-gated ATS platforms, the *first* login for a
  given (user, ATS) pair is done by the user inside a visible/remote Playwright session
  the backend launches on demand (e.g. via VNC/noVNC or a "watch and take over" mode);
  the backend only persists the resulting cookies/local-storage, encrypted at rest,
  never the password itself.
- **Never automate account creation.** If a platform requires signup, the run stops and
  becomes `manual_required`.
- **Never bypass CAPTCHA/OTP.** Detected via selector/heuristic checks → pause, notify,
  resume after the user solves it in the same persisted session.
- **Throttling is mandatory and non-configurable below safe floors**: per-character
  typing delay, per-action delay, inter-application delay, daily cap, working-hours
  window (see `automation/browser/session.py::HumanPacing`, Phase 2).
- **Autopilot (auto-submit without review) is opt-in, and only for public/no-login ATS
  platforms above a confidence threshold** — everything else is copilot (fill, human
  clicks submit) or goes to a review queue. Same decision table as the product's
  original compliance-first design:

  ```python
  def decide_action(form_result, user_settings):
      high_confidence = form_result.confidence >= 0.85
      is_public_ats = form_result.ats_type in ("greenhouse", "lever", "smartrecruiters", "ashby")
      if user_settings.autopilot_enabled and is_public_ats and high_confidence:
          return "AUTO_SUBMIT"
      if form_result.confidence < 0.6:
          return "NEEDS_REVIEW"
      return "COPILOT_REVIEW"
  ```
- **Idempotency**: `UNIQUE(user_id, job_url_hash)` on `applications` — never double-apply.
- **Session isolation**: one Playwright storage-state file per `(user_id, ats_platform)`,
  encrypted, never shared across users.

---

## 2. Database Schema

Existing tables (`users`, `resumes`, `jobs`, `match_results`) are untouched. New tables:

### `candidate_profiles` (1:1 with `users`)

| Column | Type | Notes |
|---|---|---|
| `profile_id` | String (UUID) PK | |
| `user_id` | String FK → users, unique | one profile per user |
| `full_name`, `first_name`, `last_name` | String | |
| `email`, `phone_encrypted` | String | phone stored via Fernet (`app/core/crypto.py`) |
| `location`, `city`, `state`, `country` | String | |
| `address_encrypted` | Text | encrypted |
| `linkedin_url`, `github_url`, `portfolio_url`, `website_url` | String | |
| `current_company`, `current_role` | String | |
| `years_of_experience` | Float | |
| `notice_period_days` | Integer | |
| `expected_salary`, `expected_salary_currency` | Float, String | |
| `work_authorization`, `visa_status` | String | free-text, pre-Phase-8; kept for backward compatibility |
| `work_authorized`, `requires_sponsorship` | Boolean, nullable | Phase 8 — genuinely different facts (authorized-today vs. will-need-sponsorship-eventually); never guessed from one another |
| `visa_type` | String, nullable | Phase 8 — e.g. "H1B", "F1 OPT" |
| `sponsorship_countries` | JSONB (list), nullable | Phase 8 — which countries `work_authorized` applies to |
| `preferred_locations` | JSONB (list) | |
| `remote_preference` | String | `remote`/`hybrid`/`onsite`/`no_preference` |
| `skills` | JSONB | `{programming_languages, frameworks, tools, certifications, technical_skills, soft_skills}` |
| `created_at`, `updated_at` | DateTime | |

### `education_entries` (many per profile)

`education_id` PK, `profile_id` FK cascade, `degree`, `university`, `field_of_study`,
`start_date`, `end_date`, `gpa`, `created_at`.

### `experience_entries` (many per profile)

`experience_id` PK, `profile_id` FK cascade, `company_name`, `job_title`, `start_date`,
`end_date`, `description` (Text), `skills_used` (JSONB list), `created_at`.

### `profile_documents` (resumes / cover letters / certificates / other)

| Column | Type | Notes |
|---|---|---|
| `document_id` | String PK | |
| `profile_id` | FK cascade | |
| `document_type` | String | `resume` / `cover_letter` / `certificate` / `other` |
| `label` | String | e.g. "Backend-focused resume" |
| `job_type_tag` | String, nullable | lets `ApplicationFlowManager` auto-pick a resume per job type |
| `original_filename`, `stored_path`, `file_hash` | String | mirrors `resumes` table conventions |
| `is_default` | Boolean | default doc of its `document_type` |
| `uploaded_at` | DateTime | |

### `candidate_demographics` (Phase 8 — voluntary EEO answers, 1:1 with `candidate_profiles`)

| Column | Type | Notes |
|---|---|---|
| `id` | String (UUID) PK | |
| `candidate_id` | FK → `candidate_profiles.profile_id`, unique | |
| `gender`, `veteran_status`, `disability_status`, `race_ethnicity` | String, nullable | `None` means "never asked," not "no opinion" |
| `created_at`, `updated_at` | DateTime | |

Deliberately its own table, not more columns on `candidate_profiles` — see that table's own SQLAlchemy docstring (`app/models/db_models.py::CandidateDemographics`). The only write path anywhere in the codebase is the user's own explicit `PUT /profile/demographics`; `automation/forms/answer_engine.py` reads this table but never writes to it, and never falls back to the LLM when a value here is missing.

### `applications` (tracking, §10 of spec)

| Column | Type | Notes |
|---|---|---|
| `application_id` | String PK | |
| `user_id` | FK → users | |
| `job_url` | Text | |
| `job_url_hash` | String, **unique with `user_id`** | idempotency |
| `company`, `position` | String | |
| `ats_platform` | String | from `ATSDetector` |
| `status` | String | `pending`/`processing`/`applied`/`failed`/`manual_required` |
| `applied_date` | DateTime, nullable | |
| `resume_used` | FK → `profile_documents`, nullable | |
| `failure_reason` | Text, nullable | |
| `confidence_score` | Float, nullable | from form-understanding/decision logic |
| `created_at`, `updated_at` | DateTime | |

### `automation_runs` (§14 logging/debugging)

`run_id` PK, `application_id` FK cascade, `started_at`, `finished_at`, `status`,
`screenshot_paths` (JSONB list of storage paths), `trace_path`, `error_log` (Text),
`retry_count` (Integer).

### `answer_cache` (Phase 6 — screening-question answers)

| Column | Type | Notes |
|---|---|---|
| `cache_id` | String (UUID) PK | |
| `user_id` | FK → users | |
| `question_hash` | String, **unique with `user_id`** | sha256 of the *normalized* question text |
| `question_text` | Text | original wording, kept for debugging/audit |
| `answer` | Text | |
| `source` | String | `deterministic` / `llm` (see `VALID_ANSWER_SOURCES`) |
| `confidence` | Float | |
| `created_at`, `updated_at` | DateTime | upserted, not append-only — a later run overwrites a stale answer |

Exact-match only (normalized whitespace/case/punctuation) — no semantic/embedding
similarity search yet. A pgvector-backed near-duplicate cache (reusing
`app/services/embedding_service.py`, the same way job matching does) is a natural
follow-up once this version has real usage data to justify it; see
`app/services/answer_cache_repository.py`'s module docstring.

Field-level mappings (`FieldMapper`) are **not** a DB table in v1 — they're versioned
JSON/Python config per adapter (`automation/ats/<name>/field_map.json`), since they're a
code concern (reviewed in PRs, living entirely inside `automation/`), not user data. If
per-user learned-mapping overrides become necessary later, that's a natural
`field_mapping_overrides` table addition, owned and queried by `app/` as usual.

---

## 3. Folder Structure

`app/` contains the existing project plus the Phase 1 profile system and the
Phase 4 application-tracking API. All browser/ATS automation code lives
under the top-level `automation/` module instead, for code organization —
see `MIGRATION.md` for the original folder-isolation rationale and
`automation/README.md` §"Architecture: internal domain module, not a
separate service" for the current (integrated) dependency rule: one-directional
(`app.api -> automation -> app.services/app.models/app.core/app.ai`), not a
no-`app.*`-imports wall.

```
ai-job-agent/
  app/
    api/
      auth.py            # existing
      profile.py          # NEW — Phase 1
      resumes.py, jobs.py  # existing
      applications.py      # Phase 4 — implemented: POST /applications/start,
                            #   GET /applications, GET /applications/{id},
                            #   GET /applications/{id}/runs (thin orchestrator:
                            #   fetches profile/resume, calls automation/,
                            #   persists the ApplicationRunResult via
                            #   application_repository; Phase 6 also builds
                            #   the per-run ApplicationAnswerEngine here)
    core/                  # existing (config, database, auth, crypto NEW)
    models/
      profile.py           # NEW — Pydantic schemas for profile API
      application.py        # NEW — Phase 4 — Pydantic schemas for applications API (+ job_description, Phase 6)
      db_models.py          # existing, extended with profile + Application/AutomationRun + AnswerCacheEntry (Phase 6) tables
    services/
      profile_repository.py   # NEW — Phase 1
      document_storage.py     # NEW — Phase 1 (generalized file_storage.py)
      application_repository.py  # NEW — Phase 4
      answer_cache_repository.py # NEW — Phase 6 — per-user screening-question answer cache
      ...                      # existing resume/job/matching services
  automation/               # separate top-level module — see automation/README.md
    interfaces.py           # app <-> automation integration seam (real + legacy types)
    browser/                # Phase 2
      browser_manager.py
      session.py
      selectors.py
    ats/                    # Phase 3/4
      base.py               # ATSAdapter ABC
      detector.py           # ATSDetector
      registry.py           # NEW — Phase 4: platform name -> real adapter class (or None)
      greenhouse/greenhouse_adapter.py
      lever/lever_adapter.py
      workday/workday_adapter.py
      smartrecruiters/smartrecruiters_adapter.py
      taleo/taleo_adapter.py
      icims/icims_adapter.py
      ashby/ashby_adapter.py
      bamboohr/bamboohr_adapter.py
      oracle_hcm/oracle_hcm_adapter.py
      generic/generic_adapter.py   # unknown/custom career portals
    forms/
      field_mapper.py       # Phase 5 — implemented
      answer_engine.py      # Phase 6 — implemented (ApplicationAnswerEngine)
      field_handlers.py     # Field Handler Registry — implemented (widget detection/fill/verify)
    applications/           # Phase 4 — implemented
      application_flow_manager.py
    agents/                 # Phase 6 roadmap cell, deliberately deferred — see §4 Roadmap
      job_application_agent.py
      profile_agent.py
      answer_agent.py
    workers/                # Phase 4+ (Celery/ARQ — not wired up yet; app/api/applications.py
      apply_worker.py       #   uses FastAPI BackgroundTasks in the same process for now)
      celery_app.py
    tests/                  # automation module's own tests (needs the same env vars as tests/)
  alembic/versions/         # existing, extended (app/ schema only)
  tests/                    # existing, extended (app/ only)
  MIGRATION.md              # app/ats,browser,form,applications,agents,workers -> automation/
```

---

## 4. Roadmap

| Phase | Deliverable | Status |
|---|---|---|
| 1 | Master candidate profile (personal/professional/education/experience/skills/documents) on top of existing FastAPI + Postgres + JWT auth, `app/` only | **Implemented** |
| 2 | `BrowserManager`/`session.py`/`selectors.py` — Playwright launch, persistent per-user contexts, screenshot-on-failure, trace recording, generic Next/Submit/file-upload/CAPTCHA DOM helpers | **Implemented** |
| 3 | `ATSDetector` — URL-pattern tier (all 9 platforms), DOM-fingerprint + meta-tag tier for custom domains, fallback to `custom` | **Implemented** |
| 4 | `GreenhouseAdapter`, `LeverAdapter` + `ApplicationFlowManager` (CAPTCHA short-circuit, multi-step nav with a `MAX_STEPS` safety cap, auto-submit/needs-review/copilot-review decision); `applications`/`automation_runs` DB tables + `POST /applications/start`, `GET /applications`, `GET /applications/{id}`, `GET /applications/{id}/runs` tracking API (background-task handoff into `automation/`, no queue/worker yet) | **Implemented** |
| 5 | `FieldMapper` — tiered name/id > label > placeholder > nearby-text field resolution, wired into `ATSAdapter._fill_known_questions`'s two-pass sweep (labels, then any remaining name/placeholder field with no `<label>` at all) | **Implemented** |
| 6 | `ApplicationAnswerEngine` — deterministic classifier (work authorization, notice period, salary, years of experience, straight from the profile, never fabricated) + one batched LLM call per form for genuinely subjective/novel questions, backed by a persistent per-user `answer_cache` (§2); wired into `ATSAdapter._fill_known_questions()` as the handler for labeled questions `FieldMapper` can't resolve. LangGraph agent layer (`automation/agents/*.py`) is a separate, deliberately deferred piece of this same roadmap cell — see note below. | **Implemented** (agent layer deferred) |
| — | **Field Handler Registry** (`forms/field_handlers.py`) — a widget-interaction layer between field detection and confidence calculation: auto-detects the real widget behind a field (native `<select>`, react-select, ARIA combobox/listbox, country picker, searchable/virtualized dropdown, checkbox, radio, date, file upload) and fills + verifies it through one of ten `FieldHandler` implementations behind a `FieldHandlerRegistry`, instead of ATS adapters special-casing individual field types. Every fill is verified against the DOM after filling, with up to 2 retries; unresolvable or unverifiable fields come back as a structured `FieldFailure` (`field_label`, `field_type`, `expected_value`, `actual_value`, `failure_reason`, `retry_count`) attached to `FieldFillResult.failure`, rather than a bare pass/fail. `ATSAdapter.upload_resume()` moved from an abstract per-adapter method to one concrete implementation on the base class, built on this same registry (`FileUploadHandler`). See `automation/README.md` for the full design and the two explicit scope boundaries this round didn't cross. | **Implemented** |
| 7 | Dockerized deployment (FastAPI, Postgres, Redis, worker, Playwright image), CI, remaining ATS adapters (Workday, SmartRecruiters, Taleo, iCIMS, Ashby, BambooHR, Oracle HCM, generic) | Not started |
| 8 | Compliance profile fields (`work_authorized`/`requires_sponsorship`/`visa_type`/`sponsorship_countries`) + a separate, never-LLM-guessed `candidate_demographics` table; `question_classifier.py` narrowing `ApplicationAnswerEngine`'s deterministic categories; `ToggleHandler`/`VirtualizedListboxHandler` + hardened ARIA-aware `RadioHandler`/`CheckboxHandler`; scroll/click/wait primitives extracted into `automation/utils/`; structured `FieldFailure`/`format_failure_report()` failure reporting threaded through `ATSAdapter`. See `automation/README.md`'s "Phase 8" section for the full design and its explicit scope boundaries. | **Implemented** (real ATS validation against live postings — §15 of the request — still pending, no Playwright runner available in this environment) |

**Phase 6 scope note.** The LangGraph agent layer (`JobApplicationAgent`/`ProfileAgent`/`AnswerAgent`)
originally bundled into this roadmap cell is intentionally left as an interface stub for a
follow-up pass. `app/api/applications.py::_run_application` already performs the
orchestration `JobApplicationAgent` was meant to wrap (ATS detection -> adapter resolution
-> `ApplicationFlowManager.run()` -> persistence), so introducing a LangGraph state graph on
top is a genuine, separate architectural decision (new dependency, new orchestration
paradigm for error-recovery/retries) rather than something to fold in alongside
`ApplicationAnswerEngine` — same "each phase gets its own PR/session" discipline this
roadmap already follows.

Each phase after 1 gets its own PR/session, and all of it lives in `automation/`, not
`app/` — see `MIGRATION.md` for the module-isolation rationale and `automation/README.md`
for the `interfaces.py` boundary. Phase 1 code is production-ready and merges cleanly
with the existing resume/matching backend.
