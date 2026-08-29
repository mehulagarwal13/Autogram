import pytest

from app.ai.llm.base import LLMProvider, LLMError
from app.ai.llm.registry import TASK_ROUTES, PROVIDER_FACTORIES, TaskRoute
from app.ai.llm.router import LLMRouter, LLMRouterError


class FakeProvider(LLMProvider):
    name = "fake"

    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.calls = 0

    def complete(self, *, model, prompt, system=None, temperature=0.0,
                 max_tokens=1000, json_mode=False) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise LLMError("transient failure")
        return f"response-from-{model}"


@pytest.fixture
def fake_route(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setitem(PROVIDER_FACTORIES, "fake", lambda: provider)
    monkeypatch.setitem(TASK_ROUTES, "test_task", TaskRoute(provider="fake", model="fake-model"))
    return provider


def test_unknown_task_raises():
    router = LLMRouter()
    with pytest.raises(LLMRouterError, match="Unknown LLM task"):
        router.run(task="does_not_exist", prompt="hi")


def test_routes_to_configured_provider(fake_route):
    router = LLMRouter(backoff_base_seconds=0)
    assert router.run(task="test_task", prompt="hi") == "response-from-fake-model"


def test_retries_transient_failures(monkeypatch):
    provider = FakeProvider(fail_times=2)  # fails twice, succeeds third
    monkeypatch.setitem(PROVIDER_FACTORIES, "flaky", lambda: provider)
    monkeypatch.setitem(TASK_ROUTES, "flaky_task", TaskRoute(provider="flaky", model="m"))

    router = LLMRouter(max_attempts=3, backoff_base_seconds=0)
    assert router.run(task="flaky_task", prompt="hi") == "response-from-m"
    assert provider.calls == 3


def test_gives_up_after_max_attempts(monkeypatch):
    provider = FakeProvider(fail_times=99)
    monkeypatch.setitem(PROVIDER_FACTORIES, "dead", lambda: provider)
    monkeypatch.setitem(TASK_ROUTES, "dead_task", TaskRoute(provider="dead", model="m"))

    router = LLMRouter(max_attempts=2, backoff_base_seconds=0)
    with pytest.raises(LLMRouterError, match="failed after 2 attempts"):
        router.run(task="dead_task", prompt="hi")


def test_model_override(fake_route):
    router = LLMRouter(backoff_base_seconds=0)
    assert router.run(task="test_task", prompt="hi", model="other") == "response-from-other"


# ---------------------------------------------------------------------------
# Vision support (`images`) — see LLMProvider.complete and
# automation/forms/vision_fallback.py
# ---------------------------------------------------------------------------

class VisionProvider(LLMProvider):
    """A provider that declares `images`, unlike `FakeProvider` above — which
    is itself the point of the test below it: the router must not hand an
    `images` argument to a provider (or test double) written before this
    existed."""

    name = "vision"

    def __init__(self):
        self.received = None

    def complete(self, *, model, prompt, system=None, temperature=0.0,
                 max_tokens=1000, json_mode=False, images=None) -> str:
        self.received = images
        return "ok"


def test_images_are_forwarded_when_supplied(monkeypatch):
    provider = VisionProvider()
    monkeypatch.setitem(PROVIDER_FACTORIES, "vision", lambda: provider)
    monkeypatch.setitem(TASK_ROUTES, "vision_task", TaskRoute(provider="vision", model="m"))

    LLMRouter(backoff_base_seconds=0).run(task="vision_task", prompt="p", images=[b"png-a", b"png-b"])

    assert provider.received == [b"png-a", b"png-b"]


def test_no_images_means_the_argument_is_not_passed_at_all(fake_route):
    """`FakeProvider.complete` has no `images` parameter. A router that always
    passed one would raise TypeError here — which is exactly what would happen
    to every text-only provider and every existing test double."""
    assert LLMRouter(backoff_base_seconds=0).run(task="test_task", prompt="hi") == "response-from-fake-model"


def test_openai_provider_builds_a_text_only_string_when_there_are_no_images():
    from app.ai.llm.providers.openai_provider import OpenAIProvider

    assert OpenAIProvider._user_content("just text", None) == "just text"


def test_openai_provider_puts_the_prompt_before_the_images():
    """Order is load-bearing: the prompt is what tells the model which image is
    which (see vision_fallback._SYSTEM_PROMPT's numbering)."""
    from app.ai.llm.providers.openai_provider import OpenAIProvider

    parts = OpenAIProvider._user_content("read these", [b"\x89PNG-one", b"\x89PNG-two"])

    assert [p["type"] for p in parts] == ["text", "image_url", "image_url"]
    assert parts[0]["text"] == "read these"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[1]["image_url"]["detail"] == "high"
