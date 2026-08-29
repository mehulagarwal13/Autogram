"""
Autonomous agent — general-purpose observe/decide/act browser agent.

This subpackage is intentionally independent of the deterministic,
per-ATS-adapter path (`automation/applications/application_flow_manager.py`,
`automation/ats/*`). It never branches control flow on which ATS platform
was detected — `automation/ats/detector.py::ATSDetector` may still be called
to enrich the LLM's context with a text hint ("detected platform:
greenhouse"), but that hint never changes which actions are available or
skips a reasoning step. See `AUTONOMOUS_AGENT.md` for the full design and how
this coexists with the existing platform.

Modules:
- `actions.py`    — the fixed, typed action vocabulary the LLM may choose from.
- `observer.py`   — turns a live Playwright `Page` into a compact, structured
                     `PageState` (accessibility-ish DOM summary), never raw HTML.
- `decision.py`   — the single LLM call per loop iteration; enforces the
                     system prompt verbatim and validates the returned JSON
                     against the 5 allowed decision types.
- `executor.py`   — dispatches ONE `AgentAction` via Playwright, reusing
                     `automation/utils/element_actions.py` +
                     `automation/utils/human_input.py`; contains the
                     safety-net checks (sensitive-field gate, submit-button
                     gate) that hold even if the LLM's own judgment fails.
- `loop.py`       — `AutonomousAgentLoop`, the orchestrator tying observer +
                     decision + executor + `automation.interfaces` + the
                     `AutonomousTask` repository together, including the
                     pause/resume mechanism.
- `runner.py`     — background execution (a task runs off the request
                     thread) and the in-process pause/resume signalling
                     registry.
"""
