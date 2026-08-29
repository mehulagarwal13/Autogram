# Autonomous Agent (general-purpose observe/decide/act mode)

## What this is

A second, independent way to run a job application, alongside the existing
deterministic per-ATS-adapter path (`automation/applications/application_flow_manager.py`
+ `automation/ats/*`). Where that path detects the ATS platform and branches
into a dedicated adapter (Greenhouse, Lever, Workday, ...), the autonomous
agent never branches on platform at all: every decision about what to do
next on the page comes from one LLM call per loop iteration, constrained to
a small, typed action vocabulary.

```
observe (PageState) -> decide (LLM, one of 5 structured decisions) -> act (one typed action) -> repeat
```

`automation/ats/detector.py::ATSDetector` may still run to attach a
`detected_ats_platform` **hint** string to the LLM's context (e.g.
"detected platform: greenhouse") — this is context enrichment only. It never
changes which actions are available, never skips the LLM call, and there is
no `if platform == "greenhouse":` anywhere in this subpackage.

## Why it coexists rather than replaces

The per-ATS path is faster, cheaper (mostly non-LLM field mapping), and
better-tested for the platforms it has adapters for. The autonomous agent
exists for everything else: unrecognized/custom career portals, multi-step
flows an adapter doesn't model, and as a fallback. Both write to
completely separate tables (`applications`/`automation_runs`/
`application_questions` vs. `autonomous_tasks`) and share no code path except
common browser primitives (`automation/browser/*`, `automation/utils/*`).

## Module map

| File | Responsibility |
|---|---|
| `automation/agents/autonomous/actions.py` | The closed action vocabulary (`navigate, click, fill, select, check, uncheck, scroll, press_key, upload_file, extract_text, wait, go_back, get_page_state`) and its `AgentAction`/`ActionResult` types. No "run arbitrary code" action exists. |
| `automation/agents/autonomous/observer.py` | Turns a live Playwright `Page` into a `PageState` (URL, title, visible text, a bounded list of interactive elements) via one fixed, non-LLM-authored extraction script. Elements are tagged `data-agent-ref="<n>"` so an action targets a ref, never a raw selector the model invented. |
| `automation/agents/autonomous/decision.py` | The one LLM call per iteration. Carries the **verbatim** required system prompt. Parses/validates the response into exactly one of the 5 decision types; anything else raises `DecisionError`. |
| `automation/agents/autonomous/executor.py` | Dispatches one `AgentAction`. Two safety nets independent of the LLM's own judgment: (1) refuses to fill/select/check a sensitive field (work authorization, visa, demographic, criminal history, legal declaration/attestation) unless the value is already sourced from `confirmed_answers`/profile; (2) refuses to click anything that looks like a final "Submit"/"Apply Now" control unless `auto_submit_approved` is set. |
| `automation/agents/autonomous/loop.py` | `AutonomousAgentLoop` — the orchestrator. Also verifies a `TASK_COMPLETED` claim against visible confirmation text before trusting it; downgrades an unverified claim to `APPLICATION_READY_FOR_SUBMISSION`. |
| `automation/agents/autonomous/runner.py` | Background execution + the pause/resume signalling registry. See "Background execution" below for why this is a thread, not Celery, today. |
| `app/services/autonomous_task_repository.py` | Persistence for `AutonomousTask` (`app/models/db_models.py`). |
| `app/api/autonomous_agent.py` | The `/agent/*` routes. |
| `app/api/human_interaction.py` | The `/human-requests/*` + `/agent/tasks/{id}/human-request` routes — see "OTP / MFA" below. |
| `app/services/human_interaction_repository.py` | Persistence for `HumanInteractionRequest`. |
| `frontend/src/pages/AutonomousAgent.jsx` | Start screen, live status view, intervention prompts, ready-for-submission review card. |
| `frontend/src/components/VerificationModal.jsx` | OTP/MFA verification modal (masked destination, expiry countdown, paste-able code input). |

## Persisted state (`AutonomousTask`)

One row per task: `task_id, user_id, job_url, original_objective,
candidate_profile, job_information, current_status, current_browser_state,
action_history, application_progress, human_intervention, confirmed_answers,
uploaded_documents, final_result, error`, plus `auto_submit_approved` (the
compliance flag — see below).

Status enum: `CREATED, ANALYZING_JOB, RUNNING, WAITING_FOR_HUMAN,
WAITING_FOR_APPROVAL, RESUMING, COMPLETED, FAILED, CANCELLED`. Migration:
`alembic/versions/d1e2f3a4b5c6_add_autonomous_agent_tasks.py`.

`human_intervention` is a denormalized "what's pending right now" snapshot
— `{type, reason, message, information_required, request_id, request_type,
expires_at, safe_metadata}` — kept in sync with the durable
`HumanInteractionRequest` row of the same pause (see "OTP / MFA" below;
migration: `alembic/versions/e1f2a3b4c5d6_add_human_interaction_requests.py`).

## API surface

```
POST   /agent/tasks                    start a task (job_url, optional resume_id, optional profile_overrides)
GET    /agent/tasks                    list the caller's tasks
GET    /agent/tasks/{id}               current status/state
POST   /agent/tasks/{id}/resume        resume after a non-answer intervention (login, CAPTCHA solved by hand)
POST   /agent/tasks/{id}/answer        supply {question, answer} for a pending question, then resume
POST   /agent/tasks/{id}/approve       explicit consent to submit — the ONLY place auto_submit_approved is set
POST   /agent/tasks/{id}/cancel        stop the task, release the browser

GET    /agent/tasks/{id}/human-request the task's current active (PENDING) HumanInteractionRequest, if any
GET    /human-requests/{id}            a specific request's status/metadata (never a secret value)
POST   /human-requests/{id}/respond    {action: OTP_SUBMITTED|MFA_SUBMITTED|USER_APPROVED|USER_PROVIDED_VALUE, value?}
POST   /human-requests/{id}/cancel     cancel the request (and the task, if still active)
```

## Concurrency, state-machine, and restart guarantees

Every route that can resume a paused task passes through **two atomic
conditional-`UPDATE` chokepoints**, so no combination of concurrent requests
(two `/respond` calls, `/respond` racing legacy `/resume`, a response racing
a `/cancel`) can ever resume a task twice or deliver a secret twice:

| Guard | Where | What it serializes |
|---|---|---|
| `human_interaction_repository.try_claim` | `UPDATE ... WHERE request_id = ? AND status = 'PENDING'` | Two responses to the *same request*; a response racing a cancellation. The loser gets 409 **before** `deliver_secret` / `confirmed_answers` / any resume signal is touched. |
| `autonomous_task_repository.try_claim_for_resume` | `UPDATE ... WHERE task_id = ? AND current_status = ?` | Two "resume this task" calls that don't share a request_id — i.e. the legacy `/resume`/`/answer`/`/approve` routes racing the new `/respond`. |

**Terminal states are final.** `autonomous_task_repository` raises
`TerminalTaskError` on any attempt to transition a task that is already
`COMPLETED`/`FAILED`/`CANCELLED` — so a late loop iteration, a duplicate
resume, or an error handler firing after a concurrent cancellation can never
resurrect a finished task or overwrite an honest final status with `FAILED`.
`cancel_task` is the one deliberate exception (idempotent: cancelling an
already-terminal task returns its real status rather than erroring, because
a double-click is ordinary client behavior). `loop.py` catches
`TerminalTaskError` and stops quietly rather than treating it as a crash.

**Restart behavior is explicit, and never claims a false resume.** The
in-process registry (`runner.py::_REGISTRY`) is empty by construction the
instant a new process starts, so `reconcile_orphaned_tasks_on_startup` (called
once from `app/main.py`) fails every task still sitting in `RUNNING`/`RESUMING`
— those were abandoned mid-flight by a process that died, and nothing else in
the codebase would ever pick them up again. They get an explicit error
message telling the user to start a new task, plus `automation_failed` /
`human_request_expired` audit events. `WAITING_FOR_HUMAN`/`WAITING_FOR_APPROVAL`
tasks are deliberately left alone: a human re-engaging with them is the
normal path forward, and the resume routes' `signal_resume`/`deliver_secret`
`False`-return fallback already handles a missing `TaskHandle` correctly.
A lost `pending_secret` is **never** recovered from anywhere — it never
existed outside that dead process's memory.

## Human-in-the-loop

Triggered for: authentication/OTP/MFA/CAPTCHA, missing information the agent
can't verify, ambiguous questions, sensitive confirmations (work
authorization, visa, demographic, criminal history — **never**
auto-answered), final submission approval, and any action the executor
flags as irreversible-without-approval. Detection is layered, cheapest and
most reliable first:

1. **Deterministic (`automation/agents/autonomous/observer.py::detect_blocker`,
   run every iteration, before the LLM is ever called).** HTML-native
   signals — a `type="password"` field, `autocomplete="one-time-code"`,
   name/inputmode/maxlength patterns — and DOM/accessibility text matching
   (labels, headings, button text, surrounding copy: "verification code",
   "two-factor", "check your email for the code", "verify you are human",
   "sign in to continue", ...). Maps straight to a closed request-type
   vocabulary; the LLM decision step is skipped entirely for that iteration.
2. **The LLM's own classification** (`REQUEST_HUMAN_INTERVENTION`, or a
   code-level safety-net refusal in `executor.py`) — used only when Layer 1
   found nothing. Its free-form `intervention.type` (plus an optional
   `confidence`) is normalized to the same closed vocabulary by
   `decision.py::normalize_intervention_type`; low confidence collapses to
   `UNKNOWN_BLOCKER` rather than trusting a shaky specific guess.
3. **The executor's verification-code gate** (`executor.py`'s
   `VERIFICATION_CODE_FIELD_PATTERNS`) — a last-resort backstop for the
   residual case where Layer 1's heuristics miss an unusually-marked-up
   verification field AND the LLM then proposes filling it. The write is
   refused outright (`blocked_reason="verification_code_requires_deterministic_path"`),
   the attempted value is redacted in `action_history`, and `loop.py` raises
   a normal `OTP_REQUIRED` pause. Only `loop.py::_try_consume_pending_secret`
   — the deterministic human-response path — may pass
   `verification_code_write=True` to get through this gate.

All three layers funnel through `loop.py::_pause_for_human`, which (a) creates a
durable, individually-addressable `HumanInteractionRequest` row (see below)
and (b) sets the task's `human_intervention` snapshot column so the existing
status-polling endpoints/frontend keep working. On trigger the loop blocks
in-process (`TaskHandle.resume_event`) without closing the browser — under
the default `AUTOMATION_BROWSER_MODE=cdp` this is a tab in the user's own
already-authenticated Chrome (`automation/browser/chrome_attach.py`), so
"preserve the session" is simply "don't call `.close()`". On resume, the
loop's next iteration always calls `observe_page()` again before the next
decision — it never assumes what the human changed.

### OTP / MFA: a transient secret, never persisted

A verification code is handled entirely outside the LLM path and outside the
`confirmed_answers` column (which is otherwise how a human's answer to a
pending question is stored — see "No-invention policy" below):

- **`HumanInteractionRequest`** (`app/models/db_models.py`,
  `app/services/human_interaction_repository.py`) is the durable,
  addressable record of one pause — `request_id, user_id, task_id,
  request_type, status, title, message, safe_metadata, created_at,
  expires_at, responded_at, resolved_at`. It deliberately has **no column
  that could hold a secret** — `safe_metadata` is for non-secret context
  only (a masked destination like `j***@gmail.com`, which field/button was
  detected, never a raw code).
- **`app/api/human_interaction.py`** adds `GET
  /agent/tasks/{id}/human-request`, `GET /human-requests/{id}`, `POST
  /human-requests/{id}/respond` (`{action, value?}` — `OTP_SUBMITTED` /
  `MFA_SUBMITTED` / `USER_APPROVED` / `USER_PROVIDED_VALUE`), and `POST
  /human-requests/{id}/cancel` — additive to, not a replacement for, the
  existing `/agent/tasks/{id}/resume|answer|approve` routes (both keep
  working; whichever a client uses, `_resolve_active_request` in
  `app/api/autonomous_agent.py` keeps the `HumanInteractionRequest` row in
  sync). `/respond` enforces ownership, non-expiry, and both atomic claims
  (see "Concurrency" above) before doing anything, and returns only
  `{request_id, status: "accepted"}` — **the submitted code is never echoed
  back.**
- **The legacy routes cannot be used to bypass — or to leak — a verification
  code.** While a task's active request is `OTP_REQUIRED`/`MFA_REQUIRED`, both
  `/resume` and `/answer` return 409 (`_reject_if_active_request_is_secret`).
  This matters most for `/answer`: without that guard, a user pasting their
  code into the generic free-text answer box would have written it
  permanently into `confirmed_answers`, which every subsequent
  `GET /agent/tasks/{id}` returns — exactly the leak this system exists to
  prevent. Regression-tested in
  `automation/tests/test_autonomous_agent_legacy_routes.py`.
- For `OTP_SUBMITTED`/`MFA_SUBMITTED`, the route hands the value to
  `automation/agents/autonomous/runner.py::deliver_secret`, which stores it
  **only** on the live `TaskHandle.pending_secret` (in-process memory) and
  wakes the loop. `loop.py::_try_consume_pending_secret` reads it exactly
  once, clears the slot immediately, re-observes the page to relocate the
  verification field (never trusting a stale ref from before the pause),
  fills and submits it **deterministically — never via the LLM decision
  step**, so the plaintext code never enters an LLM prompt, `action_history`,
  or a log line (the `action_history` entry for that fill is hard-coded to
  `"value": "[REDACTED]"`). If the field can't be relocated, or if the site
  rejects the code (the same OTP blocker reappears on the next observation),
  a **brand-new, independent** `HumanInteractionRequest` is raised — the
  loop never retries with a guessed value.
- If `deliver_secret` finds no live handle (the process restarted since the
  task paused), the route does **not** silently fall back to starting a
  fresh tab with a code that tab can't use — it marks the original request
  `FAILED` and raises a new `LOGIN_REQUIRED` request asking the human to
  continue manually or restart.
- Structured, non-secret audit events (`app/services/audit_log_repository.py`,
  shared with the deterministic per-ATS path via `autonomous_task_id`) are
  recorded at every stage, **metadata only, never a secret**:
  `automation_started`, `blocker_detected`, `human_request_created`,
  `automation_paused`, `human_response_received`, `automation_resuming`,
  `verification_submitted`, `verification_accepted`, `verification_rejected`,
  `verification_field_lost`, `human_request_expired`, `automation_cancelled`,
  `automation_completed`, `automation_failed`.
  `verification_accepted`/`_rejected` are decided on the observation *after*
  a code was submitted (does the same OTP blocker come back?) via
  `loop.py::_note_verification_outcome` — purely observational, never
  affecting control flow.

### Frontend

`frontend/src/pages/AutonomousAgent.jsx` branches on the (now-normalized)
`human_intervention.request_type`: `OTP_REQUIRED`/`MFA_REQUIRED` open
`frontend/src/components/VerificationModal.jsx` (masked destination, a live
expiry countdown, 6-digit paste-able input, calls the new `/respond` route);
every other type keeps using the pre-existing `InterventionCard` (relabeled
per type) against the legacy `/resume`/`/answer` routes. There is no
existing bottom-right chat widget in this app (only a toast stack and this
dedicated `/agent` page), so this HITL UI lives on the task's own status
page rather than duplicating a chat surface that doesn't exist yet.

The code lives only in the modal's local component state, and only until
submission: `handleSubmit` clears the digit array immediately after handing
it to the API, the modal is keyed on `request_id` so a rejected code's
brand-new request force-remounts it (no partially-typed digits or stale
countdown carry over), and a stale error from a previous request is cleared
when `request_id` changes. `submitting`/`expired` both disable the Continue
button, preventing duplicate submissions and submissions against an expired
request. Nothing is ever written to `localStorage`, `sessionStorage`, a URL
parameter, or any global store — a refresh loses the typed digits by design,
and the code is never re-displayed after submission.

## No-invention policy

The decision prompt is given: resume text, structured resume data, verified
profile, and this task's `confirmed_answers` — nothing else. The system
prompt instructs the model never to fabricate qualifications, experience,
legal/demographic answers, etc., and `executor.py`'s sensitive-field gate
enforces the "verified source only" half of that in code, not just prose.
A human's answer via `/answer` is stored **only** in this task's
`confirmed_answers` — never written into the global profile or
`answer_cache_repository` (that repository is untouched by this subpackage
entirely).

## Submission gating

`auto_submit_approved` defaults to `False` and is set **only** by
`POST /agent/tasks/{id}/approve`. `executor.py` refuses any click on an
element whose name matches a final-submit pattern unless that flag is set,
regardless of what the LLM decided — this is a code-level backstop, not
trust in the model's own restraint. `TASK_COMPLETED` is only ever accepted
if `PageState` after the action shows confirmation-like text
(`loop.py::CONFIRMATION_PHRASES`); otherwise it's downgraded to
`APPLICATION_READY_FOR_SUBMISSION` and surfaced for review instead.

## Background execution — deviation from the stated Celery convention, and why

`automation/workers/celery_app.py` is, as its own docstring says, still an
empty stub: no Celery app instance, no broker configuration, and no
`REDIS_URL` in `app/core/config.py`. There is nothing to hand a task to.
Rather than build a parallel, untestable Celery wiring for this one feature,
`runner.py` runs `AutonomousAgentLoop.run()` on a daemon `threading.Thread`
inside the FastAPI process, with an in-process registry
(`task_id -> TaskHandle`) providing pause/resume/cancel signalling.

**Known limitation (tracked, not hidden):** a paused task's live browser tab
lives only in that in-process registry. If the FastAPI process restarts
while a task is `WAITING_FOR_HUMAN`/`WAITING_FOR_APPROVAL`, the DB row
(and therefore the status API) survives, but resuming starts a *new* tab at
`job_url` rather than continuing the literal same page — `runner.py`'s
`signal_resume()` returns `False` in that case and the resume/answer/approve
endpoints fall back to `start_task_background()`. `runner.py`'s module
docstring has the concrete Celery migration TODO (swap the thread for
`apply_async`; replace the in-process `Event` with a Celery-visible signal,
e.g. polling `current_status` or a Redis pub/sub channel) — deliberately
scoped so that migration only touches `runner.py`, not `loop.py`.

## Testing

Eleven test files cover this subsystem. Most use fakes/mocks for the
browser, the LLM, and the DB session, so they need neither Playwright, an
LLM API key, nor Postgres. Two do not: `test_human_interaction_race_conditions.py`
(real Postgres) and `test_e2e_hitl_browser.py` (real browser + real Postgres
+ real HTTP) — see "Real end-to-end browser validation" below.

| File | Covers |
|---|---|
| `test_autonomous_actions.py` | The closed action vocabulary + validation. |
| `test_autonomous_decision.py` | LLM response parsing/validation; the 5 decision types. |
| `test_autonomous_executor.py` | All three safety gates: sensitive-field, submit-button, and the verification-code gate (LLM-decided fill refused; deterministic path allowed). |
| `test_autonomous_loop.py` | Orchestration: observe→decide→act; pause/resume; deterministic OTP detection pausing **without any LLM call**; a delivered code filled/submitted with its value redacted everywhere; a rejected code raising a brand-new independent pause (never a blind retry); a vanished field → `UNKNOWN_BLOCKER`; a CAPTCHA that replaced the OTP page reported accurately as `CAPTCHA_REQUIRED`; the executor-gate fallback pausing + redacting. |
| `test_observer_blocker_detection.py` | Deterministic Layer 1/2 detection across OTP/MFA/CAPTCHA/login/password-field/masked-destination cases. |
| `test_human_interaction_repository.py` | Request lifecycle, expiry, type validation. |
| `test_human_interaction_api.py` | Ownership (404), expiry (410), duplicate/already-answered (409), secret action vs. non-secret type (400), missing code (422), a submission that never echoes the code, and the "session no longer live" → fresh `LOGIN_REQUIRED` fallback. |
| `test_autonomous_agent_legacy_routes.py` | Legacy-route regressions: `/resume` and `/answer` both refuse to bypass a pending OTP/MFA (and so cannot leak a code into `confirmed_answers`); non-secret interventions still resume normally; duplicate `/resume` rejected; `/approve` gating; `/cancel` audit + idempotency. |
| `test_secret_leakage_audit.py` | Structural + behavioral leakage proofs (see below). |
| `test_human_interaction_race_conditions.py` | **Requires a real Postgres** (skips cleanly without one — same convention as `conftest.py`'s Chromium fixtures): two concurrent request claims → exactly one winner; two concurrent resume claims → exactly one winner; claim-after-resolved rejected; terminal task cannot be resurrected; cancel-on-terminal is a no-op; startup reconciliation fails orphaned `RUNNING` tasks and expires their requests, while leaving `WAITING_FOR_HUMAN` alone. |
| `test_e2e_hitl_browser.py` | **Real browser + real Postgres + real HTTP.** See below. |

`test_secret_leakage_audit.py` is the "prove it, don't assert it in a
docstring" file. **Structural**: `HumanInteractionRequest` has no column
whose name could hold a value/secret/code/password/token/answer (so a future
schema change that adds one fails the test); `RespondResult` exposes only
`{request_id, status}`; the GET schema has no `value` field; `_SECRET_ACTIONS`
is pinned. **Behavioral**: it runs the real `_try_consume_pending_secret` and
the real `/respond` handler with a distinctive canary code, then asserts the
canary is absent from every persisted repository write, the task row,
`action_history`, the request row, every audit event, the HTTP response body,
and `caplog` output captured at **DEBUG** level — while confirming it *did*
reach the browser field and *did* reach `deliver_secret` exactly once. It
also asserts the LLM prompt builder, fed the exact `action_history` shape the
secret path writes, contains `[REDACTED]` and not the canary.

### Real end-to-end browser validation (`test_e2e_hitl_browser.py`)

Nothing is faked: a real Playwright **Chromium**, a real local HTTP site
(`automation/tests/fixtures/hitl_test_site.py`), the real Postgres database,
the real FastAPI app/routes/JWT auth over real HTTP, and the real
`runner.py` background thread + `AutonomousAgentLoop` + `ActionExecutor`.
The LLM is stubbed only in the blocker tests (for determinism); `test_01`
deliberately uses the **real** model so a regression in the ordinary
non-blocker flow can't hide behind a stub.

**It runs in `cdp` mode — the production default** — rather than `launch`,
for two reasons. First, it exercises the code path that actually ships
(`BrowserManager._attach_over_cdp` → `chrome_attach.connect_to_chrome` →
`contexts[0]`). Second, Playwright's *sync* API is thread-affine: a `Page`
created on the loop's background thread cannot be driven from the test
thread. Production never needs to (only the loop touches the page), but
several scenarios require a **human** to act in the browser — log in, clear a
challenge, navigate away — while the automation holds its own connection.
Over CDP the test opens an independent connection to the same browser, which
is exactly the real-world situation `cdp` mode exists for.

```bash
# conftest.py sets test-only defaults for these via os.environ.setdefault,
# which win over .env — so export the real ones explicitly.
export DATABASE_URL="postgresql://...neon.tech/...?sslmode=require"
export OPENAI_API_KEY="sk-..."          # only test_01 needs it
pytest automation/tests/test_e2e_hitl_browser.py -v -s
```

Must run in its **own pytest process** (see `conftest.py::requires_chromium`:
a session-scoped `sync_playwright()` from another file collides with the
separate instance `BrowserManager` starts). Slow — tens of seconds per test,
because every step is a real page load plus real round-trips to a cloud
database. Skips cleanly when Postgres is unreachable or Chromium can't open
the DevTools port.

Two fixture details worth knowing, both learned the hard way:

- The CDP browser gets a **unique temp profile directory per run**. Chrome's
  ProcessSingleton means a second Chrome pointed at an in-use profile hands
  off its command line and exits *without opening the debug port* — so a
  fixed directory made every run after a leaked one skip. Teardown kills the
  whole process **tree** (`taskkill /T`), since renderer children outlive a
  bare kill of the parent and keep the profile locked.
- The `human` fixture holds **at most one** CDP connection at a time. An
  earlier version reconnected every poll and never closed the old client,
  leaking 60–120 DevTools connections per test; Chrome then began stalling
  new clients, which surfaced as `connect_over_cdp: Timeout`,
  `Page.goto: Timeout`, and `greenlet.error: Cannot switch to a different
  thread` in *unrelated* tests. It reconnects only when a tab it needs isn't
  visible yet (a client cannot see tabs opened after it connected).

**Three production bugs were found by this suite and fixed** (all with
unit-level regression tests):

0. **Cancelling a PAUSED task left it stuck in `WAITING_FOR_HUMAN` forever,
   and leaked the browser** (`loop.py` + both `/cancel` routes) — the most
   serious of the three, because a paused task is exactly when a user is most
   likely to cancel. `POST /agent/tasks/{id}/cancel` only persisted
   `CANCELLED` when `request_cancel()` reported *no* live handle, trusting a
   live loop to do it; but a loop blocked in `_wait_for_resume()` returned
   `False`, and every caller did `if not self._wait_for_resume(): return` —
   returning straight out of `_loop_body` and skipping `cancel_task()`, the
   `automation_cancelled` audit event, **and** `_close_browser()`. Net
   effect: the task never left `WAITING_FOR_HUMAN` (the UI showed "Waiting
   for You" indefinitely, with no way to cancel it), and each cancel leaked a
   Playwright driver process plus an open browser tab. Fixed by making the
   pause sites `continue` so the single cancellation path at the top of
   `_loop_body` owns it, and by persisting the cancellation unconditionally in
   both `/cancel` routes. Regressions:
   `test_autonomous_loop.py::test_cancelling_a_paused_task_persists_cancelled_and_releases_the_browser`
   and `test_autonomous_agent_legacy_routes.py::test_cancel_persists_cancelled_even_when_a_live_loop_handle_exists`.
   *This was found only because the leak accumulated across a full E2E run
   until later tests could no longer obtain a browser — 10 orphaned Chromium
   processes and 4 orphaned drivers were left behind by one 14-test run.*

1. **Validation ran after the destructive atomic claims** (`app/api/human_interaction.py`).
   An invalid `/respond` — a verification code sent to a `LOGIN_REQUIRED`
   request, or an empty value — returned 400/422 only *after* `try_claim`
   had consumed the request and `try_claim_for_resume` had moved the task out
   of `WAITING_FOR_HUMAN`. Net effect: a plainly-invalid call destroyed a
   legitimate pending request **and stranded the task in `RESUMING` with
   nothing able to resume it**, leaving cancel-and-restart as the only way
   out. Payload validation now happens before any claim. Regressions:
   `test_human_interaction_api.py::test_otp_action_against_a_non_secret_request_type_is_rejected`
   and the three tests beside it, which assert the request stays `PENDING`
   and the task stays `WAITING_FOR_HUMAN`.
2. **`mark_resuming` could clobber a terminal status** (same file +
   `human_interaction_repository.py`). `deliver_secret` wakes the loop
   immediately, so the loop could reach its own `mark_resolved` before the
   API thread ran its next statement; the later unconditional `RESUMING`
   write then dragged a fully-consumed request back to a non-terminal status
   permanently. Fixed by writing `RESUMING` *before* handing the secret over,
   and by making `mark_resuming` refuse to move a request that is already
   terminal. Regression:
   `test_human_interaction_repository.py::test_mark_resuming_never_clobbers_a_terminal_status`.

**One robustness hardening**, from a symptom seen once during a heavily
contended run: a Postgres **deadlock** between the loop thread and an API
thread (they touch `autonomous_tasks` and `human_interaction_requests`, and a
deadlock needs only opposite lock orders) poisons the loop's SQLAlchemy
session, after which every further statement raises `PendingRollbackError`.
`loop.py::_safe_mark_failed` was therefore unable to mark the task FAILED,
which would have left it stuck in `RUNNING` forever — the exact outcome that
function exists to prevent. It now rolls the session back first and never
lets a bookkeeping failure escape. Regressions:
`test_autonomous_loop.py::test_safe_mark_failed_still_records_on_a_poisoned_session`
and `::test_safe_mark_failed_never_raises_even_if_the_write_fails`. The
deadlock itself was not reproducible outside that pileup and is not
separately fixed; the atomic single-statement claims are already the main
defense against lock-ordering problems.

**Two frontend defects** were also fixed in `VerificationModal.jsx`: the
typed code was not cleared when the user cancelled (it survived in component
state if the cancel request itself failed), and the modal's backdrop/X was
wired straight to `onCancel` — so one stray click outside the dialog silently
**cancelled the user's whole job application**. Dismissing is now
non-destructive and clears the code; cancelling requires the explicit button.

**Actual results** (run in this environment):

```
# fakes only, no external services needed
pytest automation/tests/test_autonomous_actions.py \
       automation/tests/test_autonomous_decision.py \
       automation/tests/test_autonomous_executor.py \
       automation/tests/test_autonomous_loop.py \
       automation/tests/test_observer_blocker_detection.py \
       automation/tests/test_human_interaction_repository.py \
       automation/tests/test_human_interaction_api.py \
       automation/tests/test_autonomous_agent_legacy_routes.py \
       automation/tests/test_secret_leakage_audit.py
  -> 94 passed

# the real-Postgres concurrency/restart suite (skips without DATABASE_URL)
DATABASE_URL=... pytest automation/tests/test_human_interaction_race_conditions.py
  -> 8 passed

# the real-browser end-to-end suite, in its own process
DATABASE_URL=... OPENAI_API_KEY=... pytest automation/tests/test_e2e_hitl_browser.py
  -> 14 passed   (~11 min; every step is a real page load + cloud-DB round-trip)

cd frontend && npx vite build -> ✓ built, no errors
```

The E2E suite's own history is worth recording, since each round found
something real: **8 passed / 6 failed** on the first full run (the `human`
fixture leaked a CDP connection per poll and starved the browser), **9/5**
after fixing that (the production cancel-while-paused leak was still
orphaning a driver + tab per test — 10 Chromium and 4 driver processes
survived one 14-test run), **13/1** after fixing the leak, and **14/0** after
correcting one test that asserted on the LLM stub before the loop had
reached its next decision.

Pre-existing, unrelated: running the *entire* `automation/tests` directory in
one process yields ~350 errors from `conftest.py`'s documented
`sync_playwright()` same-thread fixture collision (its own docstring says
"Run this test file on its own, or before any file using `browser`/`page`").
Verified unrelated to this work: the identical 350 errors occur with every
file listed above excluded, and each affected file passes on its own
(e.g. `test_workday_adapter.py` → 28 passed, `test_selectors.py` → 58 passed).

**Schema note:** this repo's own convention (see the migration file
docstrings) is that `app/main.py` bootstraps the schema via
`Base.metadata.create_all()` at startup rather than the dev DB tracking
Alembic's `alembic_version` — that's how `human_interaction_requests` (a new
table) already exists. `create_all()` never alters an *existing* table,
though, so the two `application_audit_log` changes (nullable
`application_id`, new `autonomous_task_id` column/FK/index) were applied to
the dev DB directly (same DDL the migration encodes) rather than via
`alembic upgrade head`, which would also try to replay every migration this
DB already has via `create_all()` instead of Alembic. A fresh database gets
all of this from `create_all()` alone; `alembic upgrade head` is there for
whichever environment actually tracks Alembic history.

## Production-readiness review (real-workflow compatibility)

Findings from reviewing the feature against Autogram's *actual* application
workflow, rather than the controlled browser fixture.

### Deployment model: single-process, and that is compatible

Verified by inspection, not assumption: `requirements.txt` ships
`uvicorn[standard]` and nothing else that serves HTTP (no gunicorn,
hypercorn, or daphne); `README.md` says `uvicorn app.main:app --reload`
(`--reload` is single-worker by construction); and there is **no**
Dockerfile, docker-compose, Procfile, k8s manifest, CI workflow, or
`--workers`/`WEB_CONCURRENCY` reference anywhere in the repository. So the
in-memory `_REGISTRY` is correct for the deployment that exists today, and no
distributed coordination is warranted.

If a second process/replica is ever introduced, the failure mode is bounded
and already handled rather than silent: an API request landing on a process
that does not own the task's `TaskHandle` finds no live handle, so
`deliver_secret()` returns `False` (→ the request is marked `FAILED`, an
`automation_session_lost` audit event is recorded, and a fresh
`LOGIN_REQUIRED` request explains it to the user) and `signal_resume()`
returns `False` (→ `start_task_background()` begins a fresh run from the
persisted state). Every concurrency guard is a single conditional `UPDATE` in
Postgres, so those hold across processes; only the browser handle is local.

### The two paths do not interfere

`automation/applications/application_flow_manager.py` (deterministic, entered
via `POST /applications/...` in `app/api/applications.py`, adapters registered
for **greenhouse / lever / workday** only) and
`automation/agents/autonomous/` (entered via `POST /agent/tasks`) share no
tables: `applications` / `automation_runs` / `application_questions` /
`answer_cache` versus `autonomous_tasks` / `human_interaction_requests`, with
no foreign key between them. Verified by grep: the autonomous subpackage and
its API/repositories reference none of the deterministic path's repositories,
and `data-agent-ref` (the observer's element tagging) is used nowhere outside
`automation/agents/autonomous/`. HITL is therefore isolated to the autonomous
path — the deterministic path keeps its own `manual_required` /
`copilot_review` review-session mechanism.

Browser ownership does not conflict either: both construct `BrowserManager`,
each gets its own `sync_playwright()` on its own dedicated thread, each
`connect_over_cdp`s to the same Chrome and opens its **own tab**, and
`close()` closes only the tabs it opened. Multiple independent CDP clients on
one browser were exercised continuously by the E2E suite (the automation and
the simulated human each hold their own connection).

### One active automation per job (cross-path)

Previously this was possible and unguarded: `POST /agent/tasks` had **no**
duplicate check of any kind, so N calls with the same URL produced N active
tasks — and under the default `cdp` mode, N browser tabs independently filling
the same application form. Reproduced against the real database before the
fix: three concurrent `RUNNING` tasks on one URL. The deterministic path had
always protected itself (`uq_applications_user_job_url`) but knew nothing
about the autonomous path, and vice versa.

The two systems remain independent — no shared tables, no shared execution
logic, no new table, no queue. What was added is one narrow boundary that
answers a single question:

`app/services/automation_ownership.py` — *"is another active automation
already operating on this same job?"* It reads only status columns from
`autonomous_tasks` and `applications`, and holds a lock. It never starts,
stops, resumes, or inspects the internals of either path.

**Job identity reuses the existing hash.** Both paths now call
`application_repository.compute_job_url_hash` — `sha256(url.strip().lower())`,
the function the deterministic path has always used for
`Application.job_url_hash`. `AutonomousTask` gained the same `job_url_hash`
column. Sharing one implementation is what makes cross-path recognition work;
normalizing differently in each would make it silently miss. Normalization is
deliberately *only* trim + case-fold — no trailing-slash collapsing, no
fragment or query-parameter stripping — because on real career sites the query
string routinely **is** the posting identity (`?gh_jid=`, `?jobId=`, Workday
params), and merging two genuinely different postings is far worse than failing
to merge two spellings of one.

**Two layers, and both are load-bearing:**

1. `uq_autonomous_tasks_active_job` — a **partial** unique index on
   `(user_id, job_url_hash)` `WHERE current_status NOT IN ('COMPLETED',
   'FAILED', 'CANCELLED')`. This is what makes two simultaneous same-path
   inserts unable to both commit; the loser gets `IntegrityError`, which the
   route turns into the same 409. Partial is the whole point: a terminal task
   drops out of the index, so a retry after a failure or cancellation inserts
   cleanly. A plain unique constraint would have barred the job forever after
   one attempt.
2. `reserve_job_automation` — a Postgres **transaction-scoped advisory lock**
   on `(user, job)`, taken at the top of both start handlers. A unique index
   cannot span two tables, so the *cross-path* case is the one place a plain
   check-then-act could interleave; the lock closes that window. It is a
   built-in Postgres primitive (not new infrastructure) and releases
   automatically with the transaction, so it cannot leak.

**Semantics.** Active autonomous statuses are derived as
`VALID_AUTONOMOUS_TASK_STATUSES - AUTONOMOUS_TASK_TERMINAL_STATUSES` (so a new
status can never be silently omitted from the guard). On the deterministic
side the boundary reuses that route's own `IN_PROGRESS_STATUSES` — deliberately
*not* the retryable ones, since `POST /applications/start` already treats
`failed`/`manual_required`/`needs_review` as "retry on the same row". A
`COMPLETED` autonomous task does **not** block a new one: this guard is about
concurrency, not lifetime de-duplication, and permanent "already applied"
semantics stay where they already live — the deterministic path's
`COMPLETED_STATUSES` rule. Duplicating that here would have created two
contradictory duplicate rules.

**Browser safety.** The guard runs *before* `create_task` and therefore before
`start_task_background`, so a rejected duplicate allocates no task row, no
`BrowserManager`, no Playwright session, and no Chrome tab. The
`IntegrityError` backstop rolls the session back before re-querying (a failed
INSERT poisons it) and still returns 409 rather than a 500.

**API.** 409 with a structured body the frontend branches on:
`{reason: "active_automation_exists", message, path: "autonomous"|"deterministic",
status, task_id, application_id}`. `reason` is the machine-readable
discriminator; `path` + the id let the UI link straight to the run that owns
the job. Only ids the caller already owns are returned. `frontend/src/api.js`
now attaches a structured `detail` to the thrown error (it previously
`JSON.stringify`'d it into the message, surfacing raw JSON in a toast), and the
start form renders an inline notice with an "Open the run in progress" action.

Tests: `automation/tests/test_duplicate_automation_guard.py` (33, incl. a real
6-thread race against the live index and a proof that the advisory lock
genuinely blocks a second transaction) and
`test_duplicate_automation_api.py` (6, ordering + no-browser-on-rejection).
Migration: `alembic/versions/f2a3b4c5d6e7_add_autonomous_task_job_url_hash.py`
(backfills in Python via the real hash function rather than SQL, so it needs no
`pgcrypto` and cannot drift from the application's own normalization; retires
pre-existing duplicate active rows as `CANCELLED` so the index can be created).

### Lifetime duplicates — a *separate* concept from concurrency

The guard above protects concurrency only: it deliberately excludes terminal
statuses so a FAILED or CANCELLED attempt can be retried. That also let a
*successful* one through, which was a real gap — verified empirically, not
inferred:

* an autonomous task at `COMPLETED` was invisible to `POST /applications/start`
  (`find_active_automation` → `None`, `get_by_user_and_url` → `None`), so the
  deterministic path would have run a second full application;
* an application at `applied` was invisible to `POST /agent/tasks`, so the
  autonomous path would have re-applied;
* and the autonomous path creates **0** `Application` rows on success, so no
  shared "submitted" identity existed at all.

`automation_ownership.find_submitted_application` closes this. It is a second,
separate question — *"has this user already successfully submitted for this
job?"* — and both start routes now ask it after the concurrency check.

**No new marker is written for it.** It reads the two signals each path
already records only on genuine, confirmation-verified submission:

| Path | Signal | Why it means *submitted* |
|---|---|---|
| Autonomous | `current_status == 'COMPLETED'` | One call site only (`loop.py`'s `TASK_COMPLETED` branch), gated on `_page_shows_confirmation`; an unverified claim is downgraded to `WAITING_FOR_APPROVAL`. |
| Deterministic | `status == 'applied'` (+ `applied_date`) | Only via `submit_and_confirm` → `wait_for_submission_confirmation`. |

That was a deliberate choice over introducing a `submitted` flag: a new flag is
one more thing that can be written at the wrong moment (task start, partial
fill, waiting for an OTP, waiting for approval, a crash), whereas these two are
already load-bearing and already correct. `CREATED`/`RUNNING`/
`WAITING_FOR_HUMAN`/`WAITING_FOR_APPROVAL`/`FAILED`/`CANCELLED` and
`pending`/`processing`/`copilot_review`/`failed`/`manual_required`/
`needs_review`/`cancelled` are all explicitly tested as **not** submitted.

**`COMPLETED` was NOT added to the partial unique index**, on purpose. Doing so
would have given it the same semantics as FAILED/CANCELLED (which must stay
retryable) and conflated two different ideas. The index still permits the
insert; the *route* refuses it. Both behaviours are pinned by
`test_completed_does_not_block_the_partial_index_but_does_block_the_route`.

**Why no race exists between "submission completes" and "a new start".** The
active-status set and the submitted marker live on the **same row**, and
`mark_completed` (and `apply_run_result`) move between them in **one
transaction**. A concurrent start therefore observes the row either before the
commit (active → blocked by the concurrency guard) or after it (submitted →
blocked by the lifetime guard). There is no third state in which the row is
neither. Both checks additionally sit inside the same advisory-lock window.
Asserted by hammering a start-side check from another session while the
completion commits and requiring that no observation ever concluded "ALLOWED".

409 body: `{reason: "application_already_submitted", message, path,
submitted_at, task_id, application_id}` — a **different** `reason` from
`active_automation_exists`, because one is transient (offer to open the run)
and the other permanent (nothing to resume). The UI branches on it.

Still a product decision, not guarded: nothing stops a user applying to the
same job under two *different* URLs that resolve to the same posting, since
that would require aggressive normalization this deliberately avoids.

### Deliberate re-application (the explicit override)

A genuinely reposted listing, or an intentional second application, is now
possible — but only through a deliberate act. `POST /agent/tasks` accepts an
optional `acknowledge_previous_submission: {path, task_id?, application_id?}`.

Why that shape rather than `reapply: true` or a query parameter: a bare flag is
precisely what a retrying HTTP client, a sticky querystring, or stale frontend
state can set by accident. To fill this in, a client must have received the
`application_already_submitted` 409 and copied the specific id out of it — a
generic retry of the original request body cannot conjure one. It is also
**self-expiring with no token store**: the server requires it to match the
CURRENT most-recent submission for that job, so once a re-application
completes, an acknowledgement kept from the first 409 stops validating. It
authorises exactly one re-apply past one specific submission, never a standing
bypass.

Order of checks is load-bearing. The acknowledgement is consulted **after** the
active-automation check, so it relaxes the lifetime guard and *nothing else* —
an override can never bypass ownership, and a `WAITING_FOR_APPROVAL` /
`WAITING_FOR_HUMAN` task (active, not submitted) cannot be jumped. Concurrency
is unchanged: two simultaneous "Apply Again" clicks still meet the partial
unique index, and exactly one wins (tested with 5 real threads).

Nothing historical is mutated. A re-application inserts a NEW `AutonomousTask`;
the original `COMPLETED` row keeps its status and `final_result` and remains
discoverable as a submission. That works because the index is partial — a
terminal row sits outside it.

Audited on the existing append-only trail as `reapplication_authorized`, with
metadata only (job hash, prior reference, new task id, path) — never a URL,
résumé, secret, or browser state.

**Deterministic re-application is deliberately NOT implemented**, and this is a
product decision rather than an oversight. `Application` carries a *full*
`UniqueConstraint(user_id, job_url_hash)`, so a second row for the same job is
impossible; and the only re-attempt mechanism, `retry_application`, resets the
**same row** to `pending`, which would erase the `applied` status and
`applied_date` that are the historical record of the first submission.
Supporting it therefore requires either weakening that constraint or destroying
history — both prohibited. Practically, a user who wants to apply again can do
so through the autonomous path, which handles a prior deterministic submission
(acknowledge the `application_id`).

### What data the agent actually gets

`_build_candidate_profile_snapshot` gives it the profile columns
(`profile_to_dict`: contact, location, work authorization, visa, salary,
notice period, clearance, languages, highest education level, ...), the full
`resume_text`, and `parsed_resume` — which is where **education and work
history** arrive (`ParsedResume.education[]` / `.experience[]`); the
`education`/`experience` child tables are not snapshotted separately. Two
deliberate exclusions, documented on that function:

* **`CandidateDemographics`** (race/gender/veteran/disability) is withheld, so
  `executor.py`'s sensitive-field gate turns any such question into a human
  pause. The deterministic path *does* read demographics
  (`automation/forms/answer_engine.py`). This asymmetry is intentional — the
  autonomous path never fills protected-class answers on the candidate's
  behalf — and it is the one place where the agent pauses for data that does
  technically exist in the database.
* **`answer_cache` / `application_questions`** are not consulted, so a
  free-text question already answered on a *different* application is asked
  again rather than auto-answered from another context.

### Résumé upload: a real gap the fixture could not catch

`AutonomousTask.uploaded_documents` was initialised to `[]` and populated by
**nothing**, while the decision prompt told the model its `upload_file`
`file_path` "must be one of the uploaded_documents you were given". With a
permanently empty list the agent could never attach a résumé — so it stalled
at the file input essentially every real application has. The controlled
fixture has no file input, which is exactly why 14/14 E2E passes did not
surface it.

Fixed in two halves, because the missing list was also masking a security
hole:

1. `app/api/autonomous_agent.py::_build_uploadable_documents` offers the
   candidate's résumé — but only when `ResumeRecord.stored_path` resolves to
   an existing **local** file. Under `STORAGE_BACKEND=s3` that column holds an
   `s3://` URI which `set_input_files` cannot upload, so nothing is offered
   and the agent raises a clean `MANUAL_ACTION_REQUIRED` ("please attach it
   yourself") instead of erroring mid-run.
2. `ActionExecutor` gained a **fourth safety gate**: `allowed_upload_paths`.
   `file_path` comes from the LLM, and the prompt's "use only what you were
   given" was prose, not enforcement — a model could have named `.env`, an SSH
   key, or any other local file and `set_input_files` would have uploaded it
   to a third-party career site. That is arbitrary local-file exfiltration
   driven by model output, so it now gets a code-level allowlist for the same
   reason the sensitive-field and submit-button gates exist. Paths are
   compared normalized (absolute, symlink-resolved, case/separator-folded).

Tests: `automation/tests/test_autonomous_uploadable_documents.py` (9) plus 4
allowlist tests in `test_autonomous_executor.py`.

### Pause expiry is now type-appropriate

Every request type was created with the same 10-minute expiry, and
`POST /human-requests/{id}/respond` enforces expiry — so a user who took
longer than ten minutes to sign in or clear an anti-bot challenge got a 410 on
a pause that was still perfectly actionable. (The legacy `/resume` +
`/answer` routes never checked expiry, so the two response paths also
disagreed.) Only `OTP_REQUIRED` / `MFA_REQUIRED` now expire — a verification
code has a real, short lifetime on the site's side — and every other type is
created with no expiry, because signing in, solving a challenge, attaching a
file, or answering a question legitimately take a person minutes. See
`loop.py::_SHORT_LIVED_REQUEST_TYPES`; test:
`test_expiry_is_only_applied_to_verification_code_requests`.

### Observability gap closed

The "no live handle" branch in `/respond` created a fallback
`LOGIN_REQUIRED` request but emitted no audit event, so the trail showed a
response arriving and a new request appearing with nothing explaining why —
i.e. an operator could not answer *"did the browser session disappear?"*. It
now records `automation_session_lost` (with `reason=no_live_task_handle`) and
the `human_request_created` event that the loop-side path already emitted.

Full event vocabulary an operator can rely on: `automation_started`,
`blocker_detected`, `human_request_created`, `automation_paused`,
`human_response_received`, `automation_resuming`, `verification_submitted`,
`verification_accepted`, `verification_rejected`, `verification_field_lost`,
`automation_session_lost`, `human_request_expired`, `automation_cancelled`,
`automation_completed`, `automation_failed` (with a `reason`:
`browser_error`, `unexpected_error`, `task_failed`, `max_iterations_exceeded`,
`orphaned_at_startup`).

### UX: intervention types are now distinguishable

`InterventionCard` rendered the single heading "Sign-in or verification
needed" with one "I've handled it" button for `LOGIN_REQUIRED`,
`CAPTCHA_REQUIRED` **and** `MANUAL_ACTION_REQUIRED` — actively misleading for
a CAPTCHA (nothing to sign into) or a file that needs attaching. Each type now
has its own title, its own explanation of *where* to act (browser tab vs. the
input on the page), and its own call-to-action, plus the machine-readable
request type shown as a chip so a user can name it when reporting a problem.
Legacy pre-vocabulary `type` values are aliased so an in-flight task started
by an older build still renders sensibly.

## Known TODOs / not fully verified

- Celery migration path for `runner.py` (see above).
- `app/ai/llm/registry.py`'s new `autonomous_agent_decision` route is on
  `gpt-4.1-mini` like every other route in this file today — whether a
  premium model should be used for this control-flow-critical call is a
  product decision, not made here (same caveat the file already states for
  `field_reasoning`/`resume_selection`).
- End-to-end run against a real ATS site was not performed (no live browser
  exercised against a real career portal in this environment) — the
  observer's fixed extraction script (including `detect_blocker`), the
  executor's Playwright calls, and the full loop were only exercised against
  fakes/mocks in the test suite above.
- The masked-destination/OTP-field/CAPTCHA text patterns in
  `observer.py::detect_blocker` are a best-effort, deliberately broad first
  pass — real career-site copy will likely surface phrasings worth adding
  over time; false negatives there fall through to the LLM's own
  classification (Layer 2) and then the executor's verification-code gate
  (Layer 3), not a hard failure or a leak.
- **`_REGISTRY` is single-process by design.** Every concurrency guard above
  is enforced in Postgres (conditional `UPDATE`), so it holds across
  processes — but `deliver_secret`/`signal_resume` target an in-memory
  registry, so a multi-process deployment (gunicorn with >1 worker, multiple
  replicas) would route a response to a worker that may not own the task's
  handle. That surfaces as the already-handled "no live handle" path (a fresh
  `LOGIN_REQUIRED` request), never as a silent failure or a leaked secret —
  but it means this feature is correct only under the project's current
  single-process uvicorn model, the same assumption
  `app/core/middleware.py`'s in-memory rate limiter already documents.
  Fixing it properly is the same Celery/Redis migration `runner.py`'s
  docstring tracks.
- Startup reconciliation runs at import time in `app/main.py` (matching how
  `ensure_pgvector_extension`/`start_scheduler` already work there) rather
  than in a FastAPI lifespan handler. Fine for the single-process model
  above; under multiple workers each would run it, and while the operation
  is idempotent, the first worker to start would fail tasks a
  still-shutting-down worker might technically still own.
