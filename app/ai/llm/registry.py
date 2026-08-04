"""
Task routing table — the single place where "which model handles which task" lives.

Changing a model, temperature, or provider for any task is a one-line edit here.
No application code references model names or vendor SDKs.

Routing philosophy (cost-aware):
- Cheap/structured extraction tasks -> small, fast models.
- Reasoning/generation tasks (tailoring, cover letters, career advice) -> premium models.
- Non-LLM work (embeddings, skill taxonomy matching) never enters this table —
  it stays on local open-source models (sentence-transformers) or plain Python.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskRoute:
    provider: str          # key in PROVIDER_FACTORIES
    model: str
    temperature: float = 0.0
    max_tokens: int = 1000
    json_mode: bool = False


TASK_ROUTES: dict[str, TaskRoute] = {
    # --- structured extraction (cheap, deterministic) ---
    "resume_parse": TaskRoute(
        provider="openai",
        model="gpt-4.1-mini",
        temperature=0.0,
        max_tokens=2000,
        json_mode=True,
    ),
    "job_fit_analysis": TaskRoute(
        provider="openai",
        model="gpt-4.1-mini",
        temperature=0.0,
        max_tokens=500,
        json_mode=True,
    ),
    # --- generation (subjective, one batched call per form) ---
    "application_answer": TaskRoute(
        provider="openai",
        model="gpt-4.1-mini",
        temperature=0.4,           # some latitude for natural phrasing, still grounded
        max_tokens=800,            # a form-sized batch of short answers, not an essay each
        json_mode=True,            # {"answers": [...]} — see automation/forms/answer_engine.py
    ),
    # --- Phase 2 prep (see PHASE2_ARCHITECTURE.md Initiative 1) ---
    # automation/agents/*.py will call these once the LangGraph agent layer
    # is built. Registered here now so the route (and its cost/latency
    # profile) is decided up front rather than hardcoded inside an agent.
    # Left on gpt-4.1-mini for both, same as every other route above:
    # whether either of these should move to a premium model (gpt-4.1 /
    # gpt-4o) is a pending product decision, not a default made here — see
    # PHASE2_ARCHITECTURE.md §0 (the README's "premium tier" claim currently
    # describes removed functionality, not a route that exists in this file).
    "field_reasoning": TaskRoute(
        provider="openai",
        model="gpt-4.1-mini",
        temperature=0.0,
        max_tokens=600,
        json_mode=True,   # {"field_key": ..., "value": ..., "confidence": ...}
    ),
    "resume_selection": TaskRoute(
        provider="openai",
        model="gpt-4.1-mini",
        temperature=0.0,
        max_tokens=400,
        json_mode=True,   # {"document_id": ..., "confidence": ..., "reason": ...}
    ),
}


def _openai_factory():
    # Imported lazily so the vendor SDK only loads when actually needed.
    from app.ai.llm.providers.openai_provider import OpenAIProvider
    return OpenAIProvider()


# Adding a provider (Anthropic, Ollama, Groq...) = one provider file + one line here.
PROVIDER_FACTORIES = {
    "openai": _openai_factory,
}
