# Autogram — Phase 2 Architecture & Roadmap

**Status:** Design doc, pending approval before implementation begins.
**Scope:** Four initiatives selected for parallel/sequenced build-out: (1) LangGraph agent orchestration, (2) new ATS adapters, (3) autonomous apply + approval gating, (4) platform hardening (storage, migrations, scale).
**Method:** Every claim about "current state" below was verified by reading the actual source files (not the README, which has at least one confirmed inaccuracy — see §4.9). Extends `ARCHITECTURE.md`, `MIGRATION.md`, `PROJECT_REPORT.md` — none of those are being replaced.

---

## 0. Corrections to the working assumptions

Two things in the original brief don't match the code, and they change sequencing:

1. **"Browser automation has only been scaffolded" is out of date.** `automation/ats/base.py`, `detector.py`, `application_flow_manager.py`, `browser/browser_manager.py`, and `browser/session.py` are real, tested, production-shaped code — not stubs. What's actually a stub is the **LangGraph agent layer** (`automation/agents/*.py` — all three raise `NotImplementedError`) and the **Celery/Redis worker layer** (`workers/celery_app.py` is an empty comment, `workers/apply_worker.py` raises `NotImplementedError`; FastAPI `BackgroundTasks` is standing in for a real queue today).
2. **The "autonomous apply workflow" you asked me to design already has a working first version.** `ApplicationFlowManager.decide_action()` is a real confidence + platform-allowlist + opt-in decision table that returns `AUTO_SUBMIT` / `NEEDS_REVIEW` / `COPILOT_REVIEW`, and `should_keep_browser_open()` already leaves the browser open for a human to review and click submit when not on autopilot. Initiative 3 below is therefore a **hardening and exposure** task (add the missing approval API, wire up the pacing limits that are defined but unused, add audit logging), not a from-scratch build.

One more finding that isn't in scope of the four initiatives but has to be flagged because it affects Initiative 4 and the AI section of Initiative 1: **`app/services/tailoring_service.py` and `app/ai/prompts/tailoring_prompts.py` have been deleted down to a one-line "REMOVED at the owner's request" stub.** `README.md` still describes resume tailoring, cover-letter generation, and a "premium model tier" as live features. They are not. `app/ai/llm/registry.py` has exactly three registered tasks (`resume_parse`, `job_fit_analysis`, `application_answer`), all on `gpt-4.1-mini` — there is no premium-tier route defined anywhere. This needs an explicit product decision (restore tailoring, or update the README) before Initiative 1 can safely reuse "the tailoring pipeline" as a pattern — recommend resolving it in Phase A below rather than letting it linger as an undocumented landmine.

---

## Recommended sequencing

Building all four literally in parallel with one team is how you get four half-finished things. Recommended order, with rationale:

| Phase | Initiative | Why here |
|---|---|---|
| **A — foundation (do first, ~1-2 weeks)** | Platform hardening subset: Alembic drift fix, storage abstraction interface, tailoring decision, LLM router premium-tier route | Every later initiative writes to the DB or calls the LLM router or stores a file. Fixing the ground under them now is cheaper than migrating on top of drift later. None of this blocks on the other three initiatives. |
| **B — parallel with A** | New ATS adapters (Phase 7 platforms) | Purely additive, mechanical, uses an interface (`ATSAdapter`) that already works for Greenhouse/Lever. Zero dependency on Initiatives 1 or 3. Can be a different engineer's workstream starting immediately. |
| **C — after A's queue/storage pieces land** | Autonomous apply hardening: approval API, `HumanPacing` enforcement, audit trail, per-user kill switch | Needs Phase A's real task queue (Celery/Redis) to be meaningful — reviewing/approving a run that's still running inside a synchronous `BackgroundTasks` call has no real "in-flight" state to act on. |
| **D — last, biggest lift** | LangGraph agent orchestration | Should wrap the *hardened* Phase C flow, not the current one — otherwise the agent inherits the same "no queue, no audit trail" gaps and they get baked into agent tool contracts that are expensive to change later. |

If you want true parallelism, A and B can start on day one with different owners; C and D are sequenced because they each depend on what A produces.

---

## Initiative 1 — LangGraph Agent Orchestration Layer

### 1. Goal
Replace the direct, imperative call chain (`ApplicationFlowManager` calling `adapter.fill_personal_information()`, `adapter.answer_questions()`, etc. in a fixed loop) with a real agent loop for the three currently-stubbed roles: `JobApplicationAgent` (drives an application end-to-end, handling the unexpected), `ProfileAgent` (picks the right resume/cover letter for a job from the candidate's document set), `AnswerGenerationAgent` (writes screening-question answers). This is the mechanism that turns Autogram from "a form-filler with a fixed script" into "an agent that adapts when the script doesn't match the page" — the generic/unknown-ATS case is the clearest example: today `GenericAdapter` always routes to `NEEDS_REVIEW` because there's no reasoning layer; an agent that can read the accessibility tree and decide what to do is the actual fix, not a 12th hand-written adapter.

### 2. Architecture
`automation/agents/` gets real implementations built on LangGraph, but the existing deterministic machinery (`ATSAdapter` subclasses, `field_mapper.py`, `answer_engine.py`, `BrowserManager`) becomes the **tool layer** the agent calls, not code that gets deleted. Concretely:

- `JobApplicationAgent` is a LangGraph graph whose nodes are thin wrappers around existing methods: `detect_platform` (calls `ATSDetector.detect`), `select_adapter` (calls `registry.get_adapter_class`, falls back to a **reasoning node** instead of `GenericAdapter.NotImplementedError` when no adapter is registered), `fill_form` (delegates to the adapter's existing fill methods when an adapter exists), `handle_unknown_field` (new — LLM reads the DOM/accessibility snapshot for a field the deterministic `field_mapper` couldn't resolve), `decide_submission` (calls the *existing* `decide_action`/`should_keep_browser_open` — this policy layer is not being reinvented, see Initiative 3).
- `ProfileAgent.select_resume_for_job` becomes a real function: embed the job description (`embed_text`, already exposed via `automation/interfaces.py`), compare against cached embeddings of the user's `ProfileDocument` resume variants (new — resumes don't currently carry per-document embeddings, only the single "active" resume in `resumes` does), and fall back to the profile's `is_default` resume when confidence is low.
- `AnswerGenerationAgent.generate` becomes a real call through `automation/interfaces.py::generate_answer(task="application_answer", ...)` — this task route **already exists** in `TASK_ROUTES`, it's just never been called from here. It should check `answer_cache_repository` first (already exists, used elsewhere) before generating, and write back on a miss.
- State/checkpointing: LangGraph's own state persistence can piggyback on the `automation_runs` row that already gets written per step today (`ApplicationFlowManager`'s checkpointing) — don't stand up a second state store.

### 3. Components involved
`automation/agents/job_application_agent.py`, `profile_agent.py`, `answer_agent.py` (rewritten); `automation/applications/application_flow_manager.py` (becomes a caller of the agent, or the agent becomes an alternate entry point — decide per adapter-availability, see Failure Scenarios); `automation/forms/field_mapper.py`, `answer_engine.py`, `question_classifier.py` (reused as-is, called as tools); `app/services/answer_cache_repository.py` (reused); `app/ai/llm/router.py` + `registry.py` (extended, see AI impact); new `automation/agents/tools.py` (thin LangGraph tool wrappers around the above); new dependency: `langgraph`, `langchain-core` (not currently in `requirements.txt`).

### 4. Database impact
- New table `resume_document_embeddings` (or a column on `profile_documents`: `embedding_vector Vector(384)`) so `ProfileAgent` can compare job descriptions against every resume variant, not just the single active one. Additive migration, no drift risk if done through the same idempotent pattern as `ensure_vector_schema()` — but see Initiative 4 §Alembic before adding another vector column outside Alembic's tracked history.
- No schema change needed for the agent loop itself — it reuses `automation_runs`/`applications`.

### 5. API impact
No new public endpoints required for the agent to function — it's invoked internally wherever `ApplicationFlowManager` is invoked today. One new endpoint is useful for observability: `GET /applications/{id}/agent-trace` returning the LangGraph run's node-by-node trace (separate from the Playwright trace `.zip` already captured) — valuable for debugging why an agent made a decision, and for the eventual "explain this application" user-facing feature.

### 6. AI impact
This is the initiative with the most AI-layer surface area:
- **Model routing**: add explicit routes for `field_reasoning` (unknown-field interpretation — needs vision or accessibility-tree text, likely a stronger model than `gpt-4.1-mini`) and `resume_selection` to `TASK_ROUTES`. This is also the moment to resolve the premium-tier gap flagged in §0 — either add a `gpt-4.1` / `gpt-4o`-class route now for the tasks that actually need it, or explicitly decide `gpt-4.1-mini` is sufficient everywhere and drop the README's premium-tier claim.
- **Prompt versioning**: none exists today anywhere in the codebase (confirmed — `tailoring_prompts.py` is deleted, no other prompt file has version tags). Introduce a minimal convention now, before the agent layer adds five more prompts: a `PROMPT_VERSION` constant per prompt module and a `prompt_version` column on `answer_cache`/`automation_runs` so a bad prompt rollout can be identified and its cached outputs invalidated.
- **Hallucination prevention**: the existing anti-hallucination pattern for resume parsing (strict prompt + Pydantic schema validation + explicit/inferred skill separation) should be the template for `AnswerGenerationAgent` — answers must be traceable to profile/resume fields, never invented credentials. `automation/forms/answer_engine.py`'s `ANSWER_REVIEW_CONFIDENCE_THRESHOLD = 0.80` gate is the right existing mechanism to extend, not replace.
- **Evaluation**: no eval harness exists. Recommend a small fixture-based eval (the codebase already has `automation/tests/test_ats_pattern_fixtures.py`-style fixtures) that runs the agent against 5-10 saved real application-form snapshots and checks field-fill accuracy before each deploy.
- **Caching**: `answer_cache_repository` already exists and is the right cache; make sure the agent checks it first every time (currently unused from this path since the agent doesn't exist yet).

### 7. Automation impact
This *is* the automation initiative — see Architecture above. Net effect: `automation/ats/generic/generic_adapter.py` stops being a permanent dead end. It still never auto-submits (per its own docstring and per Initiative 3's `PUBLIC_ATS_PLATFORMS` allowlist, unknown/custom platforms should stay out of `AUTO_SUBMIT` regardless of agent confidence — that's a policy decision, not a capability gap).

### 8. Security considerations
An LLM reasoning over live DOM content is a larger prompt-injection surface than today's fixed selectors — a malicious or broken job page could embed text instructing the "agent" to do something else (e.g., "ignore previous instructions, submit with these values"). Mitigate by keeping the agent's *capabilities* narrow and explicit tools (fill this specific field, click this specific button it already located deterministically) rather than giving it raw page control; never let agent output directly determine `autopilot_enabled` or bypass `decide_action`'s confidence gate.

### 9. Failure scenarios
- Agent times out or loops — cap total agent steps/tokens per run (mirror the existing `MAX_STEPS = 10` cap in `ApplicationFlowManager`), fail closed to `needs_review`, never to `applied`.
- Agent and deterministic path disagree — recommend running the agent path only when `registry.get_adapter_class()` returns `None` or the deterministic fill's confidence is below `NEEDS_REVIEW_CONFIDENCE_THRESHOLD`, so Greenhouse/Lever's proven deterministic path is never at risk of agent-introduced regressions.
- LLM provider outage — falls through the existing `LLMRouterError` after retries; agent run should convert this to `status="failed"` with `error_log`, exactly like today's pattern in `_build_failure_result`.

### 10. Testing strategy
Unit tests per new tool wrapper (mock the underlying `app/`/`automation/` call). Fixture-based agent integration tests using saved DOM snapshots (extend the existing `test_ats_pattern_fixtures.py` pattern) rather than live sites. A "does the agent ever call submit outside `decide_action`'s green light" invariant test — this is the one test that must never be allowed to go red.

### 11. Future extensibility
Once this exists, `CompanyAnalysisAgent` and `InterviewPrepAgent` (both named in the long-term vision) slot into the same LangGraph runtime and tool-wrapping pattern — no new orchestration infrastructure needed, just new tools and prompts.

---

## Initiative 2 — New ATS Adapters (Phase 7 platforms)

### 1. Goal
Extend real (non-stub) coverage from Greenhouse + Lever to the remaining platforms named in the brief: Workday, Ashby, SmartRecruiters, Teamtailor, BambooHR, Jobvite, iCIMS, Oracle Recruiting, SAP SuccessFactors, LinkedIn Easy Apply, Indeed, Wellfound, Naukri.

### 2. Architecture
No new architecture — this is deliberately mechanical. `automation/ats/registry.py`'s docstring already states Workday/SmartRecruiters/Taleo/iCIMS/Ashby/BambooHR/Oracle HCM directories exist but are "deliberately NOT registered" (stub-only). The work per platform is: (a) implement the four abstract methods on `ATSAdapter` (`detect`, `fill_personal_information`, `answer_questions`, `submit_application`) using the shared concrete machinery already on the base class (`upload_resume`, `_fill_known_questions`'s 4-pass sweep, `FieldFillResult`), (b) add the platform's URL/DOM fingerprints to `ATSDetector`'s `URL_PATTERNS`/`DOM_FINGERPRINTS`, (c) register the class in `ADAPTER_REGISTRY`, (d) decide the platform's `PUBLIC_ATS_PLATFORMS` membership (Initiative 3 policy call, not a code default).

New platforms not yet represented at all in `automation/ats/` (LinkedIn Easy Apply, Indeed, Wellfound, Naukri, Teamtailor, Jobvite, SAP SuccessFactors) get new subdirectories following the exact same shape as `greenhouse/`.

Two platforms need a flag on the design, not just code: **LinkedIn Easy Apply** and **Naukri** are the two most likely to have anti-automation ToS terms and bot-detection; treat these as higher-risk-tier from day one (see Security below) rather than treating all 13 platforms as equivalent risk.

### 3. Components involved
New: `automation/ats/{workday,ashby,smartrecruiters,teamtailor,bamboohr,jobvite,icims,oracle_hcm,sap_successfactors,linkedin,indeed,wellfound,naukri}/*_adapter.py`. Modified: `automation/ats/registry.py` (registration), `automation/ats/detector.py` (fingerprints).

### 4. Database impact
None beyond what Initiative 3 needs for per-platform risk tiering (a `risk_tier` or reuse of `PUBLIC_ATS_PLATFORMS`-style allowlist — see Initiative 3 §4).

### 5. API impact
None — `POST /applications/start` already accepts any `job_url`; detection is automatic. `GET /jobs/sources` (job *ingestion* connectors) is unrelated and untouched.

### 6. AI impact
None beyond what each adapter's `answer_questions` already gets for free via the shared `_fill_known_questions`/`answer_engine` machinery.

### 7. Automation impact
Each new adapter needs its own `BrowserManager` fingerprinting quirks (Workday and Oracle Recruiting/SAP SuccessFactors are both known for iframe-heavy, multi-page-load forms — expect these two to take longer than Ashby/SmartRecruiters, which are React SPAs closer in shape to Greenhouse/Lever).

### 8. Security considerations
Workday, Taleo, iCIMS, Oracle HCM are the login-gated platforms `ARCHITECTURE.md` already calls out as requiring the persistent-session/manual-login flow (`BrowserManager.manual_login_session`) — that mechanism exists; these adapters are the first real consumers of it. Before enabling LinkedIn Easy Apply or Naukri automation, get the ToS reviewed (per the existing architecture doc's own caveat: "this is not legal advice; get a technology lawyer's review before commercial launch") — recommend building these two last and behind a feature flag, not shipping them by default.

### 9. Failure scenarios
Same pattern as Greenhouse/Lever today: any adapter failure surfaces as `ApplicationRunResult(status="failed", error_log=...)`, never a crash. Platform-specific quirk: multi-page-load platforms (Workday) need the existing `run_with_retries` treated as per-page-load, not per-application, or a single flaky page load fails the whole run unnecessarily.

### 10. Testing strategy
Mirror `automation/tests/test_greenhouse_adapter.py` / `test_lever_adapter.py` per new platform — fixture-based DOM snapshot tests, not live-site tests (already the established pattern). Add each new platform to `test_ats_registry.py` and `test_ats_pattern_fixtures.py`.

### 11. Future extensibility
Because this is purely additive against an existing interface, adding a 14th platform later costs the same as this batch — no compounding complexity, which is the reason the brief's "generic rather than platform-specific" requirement is already satisfied by the existing `ATSAdapter` contract.

---

## Initiative 3 — Autonomous Apply Workflow + Approval Gating

### 1. Goal
Harden and expose the **existing** autopilot/copilot decision system so it's production-ready for real users, not just internally correct. Today the policy (`decide_action`) and the human-review mechanism (leave-browser-open + `close_review_session`) both exist in `application_flow_manager.py`, but three things are missing: a real approval API surface, enforcement of the `HumanPacing` rate limits (defined in `browser/session.py`, never referenced by the flow manager), and an audit trail beyond the per-run `automation_runs` row.

### 2. Architecture
Add an explicit approval/control surface in `app/api/applications.py` on top of the existing `application_flow_manager` primitives — no changes to the decision table itself unless a review surfaces a bug:
- `POST /applications/{id}/approve` → looks up the open review session (`list_open_review_sessions()` already exists as a library function, just not wired to an endpoint), and for platforms where the browser was left open, either lets the human click submit in that already-open window (current default) or — for a true "approve remotely" flow — has the backend click submit programmatically once a human has approved via API (needed for any future mobile/notification-based approval UX).
- `POST /applications/{id}/reject` → `close_review_session(application_id)`, mark `status="dismissed"` or similar, log rejection reason.
- `GET /applications/reviews` → list all of a user's open `COPILOT_REVIEW`/`NEEDS_REVIEW` sessions (`list_open_review_sessions()` filtered by user) — this is the dashboard's "action needed" queue.
- Wire `HumanPacing` into `ApplicationFlowManager`/the queue consumer (Initiative 4's Celery worker is the natural place — a rate limiter belongs at the queue-consumption layer, not inside a single run): enforce `daily_application_cap`, `inter_application_delay_s_min/max`, and `working_hours_start/end` **per user**, not globally.
- Add an account-level **kill switch**: `candidate_profiles.autopilot_globally_disabled` (or a dedicated settings table) that a user or an ops admin can flip to hard-stop all autopilot runs regardless of per-run `autopilot_enabled` flags — this is the "never submit unless explicitly enabled" requirement's belt-and-suspenders backstop.

### 3. Components involved
`app/api/applications.py` (new endpoints), `automation/applications/application_flow_manager.py` (call `HumanPacing` checks before starting a run; no change to `decide_action` itself), `automation/browser/session.py` (`HumanPacing` — enforce, don't redefine), new `app/services/application_repository.py` functions (`get_open_reviews_for_user`), new audit table (see DB impact).

### 4. Database impact
- New table `application_audit_log`: `log_id` PK, `application_id` FK, `user_id` FK, `event_type` (e.g. `autopilot_run_started`, `human_approved`, `human_rejected`, `kill_switch_triggered`), `actor` (`system`/`user_id`/`admin_id`), `metadata` JSONB, `created_at`. This is distinct from `automation_runs` (which tracks execution mechanics) — audit log tracks *decisions and approvals*, which compliance/support will need independently of debugging a Playwright trace.
- New column `candidate_profiles.autopilot_globally_disabled` (Boolean, default `False`) for the kill switch.
- Optional: `PUBLIC_ATS_PLATFORMS` (currently a hardcoded `frozenset` in code) becomes a DB-backed `ats_risk_tiers` table if you expect to change platform risk classification without a deploy — recommend keeping it in code for now (it's a rare, deliberate change) and revisiting only if Initiative 2's LinkedIn/Naukri risk review demands more frequent tuning.

### 5. API impact
Three new endpoints as above (`/approve`, `/reject`, `/reviews`). `POST /applications/start`'s existing `autopilot_enabled` semantics are unchanged — this initiative adds control *after* a run reaches a review state, it doesn't change how autopilot is requested.

### 6. AI impact
None directly — this initiative is policy/control-plane, not AI. It does gate Initiative 1's agent (the agent must respect the same kill switch and `decide_action` gate — call this out explicitly in Initiative 1's implementation so nobody accidentally gives the agent a path around it).

### 7. Automation impact
`HumanPacing`'s daily cap and working-hours window are currently inert — this is the initiative that makes them real, which materially changes automation *volume* (a user can't be run through 50 applications in one hour once this ships, by design — confirm this is the intended product behavior before shipping, since it's a real behavior change, not just internal hardening).

### 8. Security considerations
The kill switch must be effective even mid-run (a run already inside `_run_on_page` shouldn't ignore a kill switch flipped seconds ago) — check it at the top of every step in the `MAX_STEPS` loop, not just at dispatch time. Audit log entries must be immutable/append-only (no update/delete path) since it's the record of "did the system submit something without explicit permission."

### 9. Failure scenarios
Kill switch check itself fails (DB unreachable mid-run) — fail closed (treat as "kill switch engaged," not "assume disabled") given the explicit product requirement to never auto-submit without permission. Approval API called for a review session that already timed out/closed — return a clear 404/409, don't silently no-op.

### 10. Testing strategy
State-machine tests for `decide_action` (already testable, extend existing coverage) plus new tests for: kill switch mid-run interruption, `HumanPacing` cap enforcement across a simulated day, approval/reject API idempotency (double-approve, approve-after-timeout).

### 11. Future extensibility
The audit log table is also the foundation for a future "why did/didn't this get submitted" user-facing explanation feature, and for any compliance reporting a B2B/enterprise tier of Autogram would eventually need.

---

## Initiative 4 — Platform Hardening (Storage, Migrations, Scale)

### 1. Goal
Close three concrete, verified gaps before scaling toward "hundreds of thousands of users": no object storage abstraction (local disk only, confirmed no `boto3` anywhere), real and confirmed drift between Alembic migration history and the live ORM schema, and no real task queue (Celery/Redis are stub files; `BackgroundTasks` is standing in).

### 2. Architecture
Three independent tracks, sequenced by risk:

**Storage abstraction (do first — blocks horizontal scaling of the API itself).** Introduce a `StorageBackend` protocol (`save(path, content) -> str`, `read(path) -> bytes`, `delete(path) -> bool`, `url_for(path) -> str | None`) with two implementations: `LocalStorageBackend` (wraps today's exact behavior, zero regression) and `S3StorageBackend` (boto3, S3-compatible — works against AWS S3, Cloudflare R2, or MinIO for self-hosted). `file_storage.py` and `document_storage.py` both currently do raw `Path`/`open()` calls with no indirection — refactor both to call through the backend interface, selected via a new `STORAGE_BACKEND` config value (`local` default, `s3` when configured). This is a strict prerequisite for running more than one API instance (local disk isn't shared across instances) and for the Playwright automation module's screenshots/traces, which currently also write to local `AUTOMATION_LOGS_DIR`/`AUTOMATION_SESSION_DIR` and would break the same way under multi-instance deployment.

**Migration baseline reconciliation (do second — data-integrity risk, not availability risk).** The confirmed drift (`jobs.salary_min/salary_max/min_years_required` and `match_results.vector_similarity/skill_overlap_ratio/blended_score/ats_score/ats_format_score` created as `String` in migrations vs. `Float` in `db_models.py`, plus `embedding_vector`/HNSW never represented in Alembic at all) means a database built purely from `alembic upgrade head` — never having booted the app once — would be subtly wrong. Today this is masked because `app/main.py` runs `Base.metadata.create_all()` unconditionally before Alembic-tracked changes matter, so every real environment has gotten correct types from the ORM model, not from migrations. That masking stops working the moment you need a genuinely reproducible environment (CI, staging teardown/rebuild, disaster recovery) that doesn't rely on booting the exact same app version first. Fix: generate a fresh baseline migration (`alembic revision --autogenerate -m "baseline_reconciled"` after deleting or archiving the drifted history, exactly as `README.md` already recommends doing "when you next need a migration") that captures the *actual* current schema including `embedding_vector`/HNSW, so `alembic upgrade head` alone becomes trustworthy.

**Real task queue (do third — enables Initiative 3's pacing and Initiative 1's longer-running agent loops).** Instantiate `automation/workers/celery_app.py` for real (Celery + Redis, per the module's own deferred-comment plan) and implement `apply_worker.py::run_application` as the actual Celery task, replacing `app/api/applications.py`'s `BackgroundTasks.add_task` call. This isn't just a nice-to-have at scale — `BackgroundTasks` runs in-process with no retry/backoff, no cross-instance visibility, and no way to enforce `HumanPacing`'s per-user daily cap across API restarts.

### 3. Components involved
New: `app/services/storage/` (backend protocol + two implementations), `automation/workers/celery_app.py` (real), `automation/workers/apply_worker.py` (real). Modified: `app/services/file_storage.py`, `app/services/document_storage.py`, `app/api/applications.py` (dispatch to Celery instead of `BackgroundTasks`), `app/core/config.py` (`STORAGE_BACKEND`, `S3_BUCKET`/`S3_ENDPOINT`/`AWS_*` or equivalent, `REDIS_URL`), `requirements.txt` (`boto3`, `celery`, `redis`), `alembic/versions/` (new reconciled baseline).

### 4. Database impact
No schema change from the storage or queue work (stored paths/keys are already strings; an S3 key is just a different string shape than a local path). The migration-baseline work is itself the DB-impact item — see Architecture above. One incidental fix worth bundling: add the HNSW index that's currently missing on `resumes.embedding_vector` (`jobs.embedding_vector` has one, `resumes` doesn't — likely an oversight since resume-side vector search isn't on the current hot path, but cheap to fix while touching `pgvector_setup.py` for the baseline work anyway).

### 5. API impact
None for the queue/storage swap (both are internal implementation details behind existing endpoints). Health check (`/health`) should be extended to report Redis reachability once Celery is real, matching its existing DB-reachability check.

### 6. AI impact
None directly, though a real queue is what makes Initiative 1's longer agent runs (which may take meaningfully longer than today's synchronous flow) safe to run without tying up a web worker process.

### 7. Automation impact
Every automation run currently executes inside the FastAPI process via `BackgroundTasks` — under load this competes with request-handling capacity and has no isolation (one runaway Playwright process can degrade API latency for everyone). Moving to Celery workers gives automation runs their own process pool, which is a real reliability improvement independent of the other three initiatives.

### 8. Security considerations
S3 backend needs bucket policies scoped to least-privilege (write-only for upload paths where possible, signed URLs with short expiry for any read access to resumes/PII documents — never public-read). Redis for Celery needs auth/TLS if it's reachable outside a private network; Celery task payloads carrying `job_url`/`application_id` are fine to queue in plaintext, but never queue raw PII (profile data) as task arguments — pass IDs and re-fetch from the encrypted DB inside the worker, which is already the pattern `automation/interfaces.py` establishes.

### 9. Failure scenarios
S3 backend unreachable — must fail the specific upload/read cleanly (existing `save_resume_file`/`save_document_file` error contract preserved), not crash the request. Migration baseline cutover — must be done with a maintenance-window runbook (dump schema, verify the new baseline's `alembic upgrade head` against a scratch DB matches `create_all`'s output exactly, before touching any real environment). Redis/Celery outage — `POST /applications/start` should surface a clear 503 rather than silently accepting a request that will never be processed.

### 10. Testing strategy
`StorageBackend` protocol gets a shared test suite run against both implementations (`LocalStorageBackend` today, `S3StorageBackend` against MinIO in CI) so behavior parity is enforced, not assumed. Migration baseline gets a CI job that builds a database from `alembic upgrade head` alone (no `create_all`) and diffs its schema against one built via `create_all`, so drift regressions are caught automatically going forward. Celery task tests run with `CELERY_ALWAYS_EAGER`-equivalent synchronous execution in the existing test suite, plus one integration test against a real Redis in CI.

### 11. Future extensibility
Once `StorageBackend` exists, CDN-fronted resume/document delivery and per-tenant bucket isolation (useful if Autogram ever offers a B2B/enterprise tier) are additive, not architectural changes. Once Celery is real, it's also the natural home for the scheduler's job-sync (`app/core/scheduler.py`'s APScheduler could eventually move to Celery Beat for consistency, though that's not required by this phase).

---

## Summary of new dependencies

`langgraph`, `langchain-core` (Initiative 1); none (Initiative 2 — reuses existing base classes); none new beyond what Initiative 4 adds (Initiative 3); `boto3`, `celery`, `redis` (Initiative 4).

## What should explicitly NOT change

Per the "don't rebuild what works" mandate: `ATSAdapter`'s interface, `ATSDetector`'s tiered detection, `BrowserManager`'s session/context lifecycle, `decide_action`'s decision table, the `applications`/`automation_runs` schema, and the Greenhouse/Lever adapters themselves are all solid and should be extended, not rewritten, by every initiative above.
