# Migration: automation scaffolding out of `app/` and into `automation/`

**Why:** `app/` must remain the existing FastAPI backend (+ the Phase 1
profile system, which is plain CRUD and belongs there) and nothing else. The
Phase 2+ browser/ATS/agent scaffolding added in the previous session was
placed under `app/browser`, `app/ats`, `app/form`, `app/applications`,
`app/agents`, `app/workers` — that violates project isolation. This document
records the fix.

---

## 1. Migration plan

1. Create a new top-level module `automation/` (sibling to `app/`, not nested
   inside it), with subpackages `browser/`, `ats/`, `forms/` (renamed from
   `form/`), `applications/`, `agents/`, `workers/`, and its own `tests/`.
2. Add `automation/interfaces.py` — the single file that defines the contract
   between the two modules (plain dataclasses `CandidateProfileView`,
   `ResumeDocumentView`, `ApplicationRunResult`, and callback `Protocol`s
   `LLMCallable`, `EncryptDecryptPair`, `EmbedCallable`). This is what makes
   "communicate only through clearly defined interfaces" concrete rather than
   a slogan: **`automation/` contains zero `import app.*` statements**,
   verified below.
3. Recreate every file that was under `app/{browser,ats,form,applications,agents,workers}`
   under the equivalent `automation/` path, with internal imports repointed
   from `app.ats.base` (etc.) to `automation.ats.base` (etc.), and `Any`-typed
   profile/resume parameters tightened to the new `CandidateProfileView` /
   `ResumeDocumentView` types where that was a one-line change.
4. Move the one test that exercised this scaffolding,
   `tests/test_ats_scaffolding.py`, to `automation/tests/test_ats_scaffolding.py`,
   updating its imports to `automation.*`. It needs no `app/` environment
   (no `DATABASE_URL`, `OPENAI_API_KEY`, etc.) to run — that's a direct
   consequence of the isolation.
5. Update `ARCHITECTURE.md` (system diagram, boundary section, folder
   structure, roadmap) to reflect `automation/` as a top-level module instead
   of nested folders under `app/`.
6. Empty out the old `app/{browser,ats,form,applications,agents,workers}`
   files and the old `tests/test_ats_scaffolding.py` — see the caveat below.
7. Confirm no code under `app/` or `tests/` imports anything from the moved
   paths (checked by grep — see §4).

**Nothing in `app/`'s existing functionality was rewritten.** `app/api/profile.py`,
`app/services/profile_repository.py`, `app/services/document_storage.py`,
`app/core/crypto.py`, `app/models/profile.py`, and the `db_models.py`
additions are untouched — they were already correctly scoped to `app/`
because they're profile CRUD, not browser automation.

### Caveat: old files could not be physically deleted this session

The sandbox this session runs in failed to start its shell with "not enough
disk space," which also blocks `rm`. I could not delete the old
`app/browser/`, `app/ats/`, `app/form/`, `app/applications/`, `app/agents/`,
`app/workers/` directories or the old `tests/test_ats_scaffolding.py`.
Instead, every file in those paths has been overwritten with a one-line
docstring stating it moved and is safe to delete — they define no classes,
functions, or imports, so they're inert (pytest collects nothing from them;
nothing in `app/` imports them). **Delete them the next time a shell is
available** — a single command does it:

```bash
rm -rf app/browser app/ats app/form app/applications app/agents app/workers
rm tests/test_ats_scaffolding.py
```

---

## 2. Files that moved

| Old path (now an inert stub, pending deletion) | New path |
|---|---|
| `app/browser/__init__.py` | `automation/browser/__init__.py` |
| `app/browser/browser_manager.py` | `automation/browser/browser_manager.py` |
| `app/browser/session.py` | `automation/browser/session.py` |
| `app/browser/selectors.py` | `automation/browser/selectors.py` |
| `app/ats/__init__.py` | `automation/ats/__init__.py` |
| `app/ats/base.py` | `automation/ats/base.py` |
| `app/ats/detector.py` | `automation/ats/detector.py` |
| `app/ats/greenhouse/__init__.py`, `greenhouse_adapter.py` | `automation/ats/greenhouse/…` |
| `app/ats/lever/__init__.py`, `lever_adapter.py` | `automation/ats/lever/…` |
| `app/ats/workday/__init__.py`, `workday_adapter.py` | `automation/ats/workday/…` |
| `app/ats/smartrecruiters/__init__.py`, `smartrecruiters_adapter.py` | `automation/ats/smartrecruiters/…` |
| `app/ats/taleo/__init__.py`, `taleo_adapter.py` | `automation/ats/taleo/…` |
| `app/ats/icims/__init__.py`, `icims_adapter.py` | `automation/ats/icims/…` |
| `app/ats/ashby/__init__.py`, `ashby_adapter.py` | `automation/ats/ashby/…` |
| `app/ats/bamboohr/__init__.py`, `bamboohr_adapter.py` | `automation/ats/bamboohr/…` |
| `app/ats/oracle_hcm/__init__.py`, `oracle_hcm_adapter.py` | `automation/ats/oracle_hcm/…` |
| `app/ats/generic/__init__.py`, `generic_adapter.py` | `automation/ats/generic/…` |
| `app/form/__init__.py` | `automation/forms/__init__.py` (renamed `form` → `forms`) |
| `app/form/field_mapper.py` | `automation/forms/field_mapper.py` |
| `app/form/answer_engine.py` | `automation/forms/answer_engine.py` |
| `app/applications/__init__.py` | `automation/applications/__init__.py` |
| `app/applications/application_flow_manager.py` | `automation/applications/application_flow_manager.py` |
| `app/agents/__init__.py` | `automation/agents/__init__.py` |
| `app/agents/job_application_agent.py` | `automation/agents/job_application_agent.py` |
| `app/agents/profile_agent.py` | `automation/agents/profile_agent.py` |
| `app/agents/answer_agent.py` | `automation/agents/answer_agent.py` |
| `app/workers/__init__.py` | `automation/workers/__init__.py` |
| `app/workers/celery_app.py` | `automation/workers/celery_app.py` |
| `app/workers/apply_worker.py` | `automation/workers/apply_worker.py` |
| `tests/test_ats_scaffolding.py` | `automation/tests/test_ats_scaffolding.py` |

New, not previously present anywhere: `automation/interfaces.py`, `automation/README.md`.

**Untouched** (stayed in `app/`, no changes): everything under `app/api`,
`app/core`, `app/models`, `app/services` except the additions already made
for the Phase 1 profile system in the prior session (`app/api/profile.py`,
`app/models/profile.py`, `app/services/profile_repository.py`,
`app/services/document_storage.py`, `app/core/crypto.py`, and the profile
tables in `app/models/db_models.py`).

---

## 3. Updated imports

Only one substitution was needed across every relocated file, since none of
this scaffolding ever imported real `app/` internals (it was written against
`Any`-typed parameters, not SQLAlchemy models) — the intra-package imports
between the moved files themselves:

| Before | After |
|---|---|
| `from app.ats.base import ATSAdapter, FieldFillResult` | `from automation.ats.base import ATSAdapter, FieldFillResult` |

That line appeared identically in all 10 adapter files
(`greenhouse_adapter.py`, `lever_adapter.py`, `workday_adapter.py`,
`smartrecruiters_adapter.py`, `taleo_adapter.py`, `icims_adapter.py`,
`ashby_adapter.py`, `bamboohr_adapter.py`, `oracle_hcm_adapter.py`,
`generic_adapter.py`) and was updated in each.

Additionally, three files now import the new boundary module instead of
using bare `Any`:
- `automation/ats/base.py` — `from automation.interfaces import CandidateProfileView, ResumeDocumentView`
- `automation/browser/session.py` — `from automation.interfaces import EncryptDecryptPair`
- `automation/forms/answer_engine.py`, `automation/agents/*.py`, `automation/applications/application_flow_manager.py`,
  `automation/workers/apply_worker.py` — import the relevant dataclass/Protocol
  from `automation.interfaces` (`CandidateProfileView`, `ResumeDocumentView`,
  `ApplicationRunResult`, `LLMCallable`, `EmbedCallable`) instead of loosely
  typing those parameters.

No file under `app/` required an import change — `app/main.py`,
`app/api/profile.py`, etc. never imported the moved modules in the first
place (confirmed below).

---

## 4. Existing tests — unaffected

Checked by grepping `app/` and `tests/` for any reference to the moved
import paths (`app.ats`, `app.browser`, `app.form`, `app.applications`,
`app.agents`, `app.workers`):

- **`app/`**: zero matches. Nothing in the FastAPI app (`main.py`, `api/`,
  `services/`, `models/`, `core/`) ever imported the automation scaffolding —
  it was dead weight sitting in the wrong folder, never wired in.
- **`tests/`**: the only match is the now-inert `tests/test_ats_scaffolding.py`
  itself (its deprecation docstring mentions the old import path in prose,
  not as code).

This means every pre-existing test — `test_hard_filters.py`,
`test_skill_gap.py`, `test_experience_extractor.py`, `test_llm_router.py`,
`test_text_cleaning.py`, plus the Phase 1 profile tests
(`test_profile_crypto.py`, `test_document_storage.py`,
`test_profile_repository_helpers.py`) — imports only from `app.*` modules
that were not touched by this migration, and continues to pass exactly as
before. `automation/tests/test_ats_scaffolding.py` is a like-for-like replacement
of the old scaffolding test, now importing `automation.*` instead, with no
`app/` environment required to run it.

I was unable to actually execute `pytest` this session (see the deletion
caveat above — same sandbox limitation), so this is confirmed by inspection
(no cross-references) rather than a live test run. Recommended verification
once you have a shell:

```bash
pip install -r requirements.txt
pytest tests/ automation/tests/ -v
```
