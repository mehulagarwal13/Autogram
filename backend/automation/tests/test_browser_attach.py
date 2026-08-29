"""
Attaching to the user's real Chrome — `automation/browser/chrome_attach.py` and
the browser-mode selection in `automation/browser/browser_manager.py`.

Everything here runs without a browser: the point of these tests is the
*decision* logic (which mode wins, what gets closed, whose cookies get
exported), and the two things that must never happen no matter what — creating
an incognito context inside the user's Chrome, and closing a browser we don't
own. Both are asserted directly against fakes rather than inferred from a live
Chrome, which no CI machine has running with a debug port anyway.
"""

import pytest

from automation.browser import chrome_attach
from automation.browser.browser_manager import BrowserAutomationError, BrowserManager
from automation.browser.chrome_attach import (
    AttachedChrome,
    ChromeAttachError,
    attach_or_launch_chrome,
    cdp_port,
    connect_to_chrome,
    devtools_version,
    normalize_cdp_url,
)
from automation.browser.session import SessionStore


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakePage:
    def __init__(self):
        self.closed = False

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True


class _FakeContext:
    """Stands in for a BrowserContext. `pages` mimics the tabs the user already
    had open when we attached."""

    def __init__(self, *, existing_pages=0, storage_state_value=None):
        self.pages = [_FakePage() for _ in range(existing_pages)]
        self.closed = False
        self.new_pages: list[_FakePage] = []
        self._storage_state_value = storage_state_value or {"cookies": [{"name": "everything"}]}

    def new_page(self):
        page = _FakePage()
        self.new_pages.append(page)
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True

    def storage_state(self):
        return self._storage_state_value


class _FakeBrowser:
    def __init__(self, contexts):
        self.contexts = contexts
        self.closed = False
        self.new_context_calls: list[dict] = []

    def new_context(self, **kwargs):
        self.new_context_calls.append(kwargs)
        context = _FakeContext()
        self.contexts.append(context)
        return context

    def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, *, cdp_browser=None, launched_browser=None):
        self._cdp_browser = cdp_browser
        self._launched_browser = launched_browser
        self.connect_calls: list[str] = []
        self.launch_calls: list[dict] = []

    def connect_over_cdp(self, endpoint, **kwargs):
        self.connect_calls.append(endpoint)
        if self._cdp_browser is None:
            raise chrome_attach.PlaywrightError("nothing to connect to")
        return self._cdp_browser

    def launch(self, **kwargs):
        self.launch_calls.append(kwargs)
        return self._launched_browser


class _FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium
        self.stopped = False

    def stop(self):
        self.stopped = True


def _install_fake_playwright(monkeypatch, chromium) -> _FakePlaywright:
    """Replaces `sync_playwright()` inside browser_manager only — no driver
    process, no browser, no asyncio-loop conflict with other test modules."""
    playwright = _FakePlaywright(chromium)
    monkeypatch.setattr(
        "automation.browser.browser_manager.sync_playwright",
        lambda: type("_CM", (), {"start": staticmethod(lambda: playwright)})(),
    )
    return playwright


def _manager(tmp_path, **kwargs) -> BrowserManager:
    kwargs.setdefault("browser_mode", "cdp")
    return BrowserManager(
        user_id="user-1",
        ats_platform="greenhouse",
        session_store=SessionStore(base_dir=tmp_path / "sessions"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# CDP endpoint plumbing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://127.0.0.1:9222", "http://127.0.0.1:9222"),
        ("http://127.0.0.1:9222/", "http://127.0.0.1:9222"),
        ("127.0.0.1:9222", "http://127.0.0.1:9222"),
        ("localhost:9333", "http://localhost:9333"),
        ("9222", "http://127.0.0.1:9222"),
        ("", "http://127.0.0.1:9222"),
        (None, "http://127.0.0.1:9222"),
    ],
)
def test_normalize_cdp_url_absorbs_the_shapes_people_actually_write(raw, expected):
    assert normalize_cdp_url(raw) == expected


def test_cdp_port_reads_the_port_back_out():
    assert cdp_port("localhost:9333") == 9333
    assert cdp_port("") == chrome_attach.DEFAULT_CDP_PORT


def test_devtools_version_is_none_when_nothing_is_listening():
    # Port 1 is privileged and never a DevTools endpoint: a refused connection
    # must read as "not attachable", not raise.
    assert devtools_version("http://127.0.0.1:1", timeout_s=0.5) is None


def test_devtools_version_returns_the_payload(monkeypatch):
    class _Response:
        def read(self):
            return b'{"Browser": "Chrome/131.0.0.0"}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(chrome_attach.urllib.request, "urlopen", lambda *a, **k: _Response())
    assert devtools_version("http://127.0.0.1:9222") == {"Browser": "Chrome/131.0.0.0"}


# ---------------------------------------------------------------------------
# connect_to_chrome / attach_or_launch_chrome
# ---------------------------------------------------------------------------

def test_connect_to_chrome_reuses_the_existing_default_context():
    """The whole feature in one assertion: we hand back `contexts[0]` — the
    user's profile-backed context — and never call `new_context()`, which over
    CDP would be a fresh incognito cookie jar with none of their logins."""
    existing = _FakeContext(existing_pages=4)  # Gmail, LinkedIn, GitHub, ChatGPT
    browser = _FakeBrowser([existing])
    playwright = _FakePlaywright(_FakeChromium(cdp_browser=browser))

    attached = connect_to_chrome(playwright, "127.0.0.1:9222", version={"Browser": "Chrome/131"})

    assert attached.context is existing
    assert browser.new_context_calls == []
    assert attached.launched_by_us is False
    assert attached.cdp_url == "http://127.0.0.1:9222"
    assert attached.browser_label == "Chrome/131"


def test_connect_to_chrome_rejects_a_browser_with_no_context():
    browser = _FakeBrowser([])
    playwright = _FakePlaywright(_FakeChromium(cdp_browser=browser))

    with pytest.raises(ChromeAttachError):
        connect_to_chrome(playwright, "127.0.0.1:9222")
    # It disconnected instead of leaving a dangling connection behind.
    assert browser.closed is True


def test_attach_prefers_an_already_running_chrome_and_never_launches_one(monkeypatch):
    existing = _FakeContext(existing_pages=2)
    browser = _FakeBrowser([existing])
    playwright = _FakePlaywright(_FakeChromium(cdp_browser=browser))
    monkeypatch.setattr(chrome_attach, "devtools_version", lambda *a, **k: {"Browser": "Chrome/131"})

    def _must_not_launch(**kwargs):
        raise AssertionError("Chrome was already listening — nothing should have been started")

    monkeypatch.setattr(chrome_attach, "launch_chrome_with_remote_debugging", _must_not_launch)

    attached = attach_or_launch_chrome(
        playwright, cdp_url="http://127.0.0.1:9222", user_data_dir="unused", autolaunch=True,
    )
    assert attached.context is existing
    assert attached.launched_by_us is False


def test_attach_launches_chrome_when_the_port_is_closed(monkeypatch, tmp_path):
    existing = _FakeContext()
    browser = _FakeBrowser([existing])
    playwright = _FakePlaywright(_FakeChromium(cdp_browser=browser))
    monkeypatch.setattr(chrome_attach, "devtools_version", lambda *a, **k: None)
    launched: list[dict] = []

    def _launch(**kwargs):
        launched.append(kwargs)
        return {"Browser": "Chrome/131"}

    monkeypatch.setattr(chrome_attach, "launch_chrome_with_remote_debugging", _launch)

    attached = attach_or_launch_chrome(
        playwright, cdp_url="http://127.0.0.1:9222", user_data_dir=tmp_path / "profile", autolaunch=True,
    )
    assert attached.launched_by_us is True
    assert len(launched) == 1


def test_attach_refuses_to_launch_when_autolaunch_is_off(monkeypatch):
    playwright = _FakePlaywright(_FakeChromium())
    monkeypatch.setattr(chrome_attach, "devtools_version", lambda *a, **k: None)

    with pytest.raises(ChromeAttachError, match="AUTOMATION_CDP_AUTOLAUNCH"):
        attach_or_launch_chrome(
            playwright, cdp_url="http://127.0.0.1:9222", user_data_dir="unused", autolaunch=False,
        )


# ---------------------------------------------------------------------------
# BrowserManager: mode selection
# ---------------------------------------------------------------------------

def test_unknown_browser_mode_fails_at_construction(tmp_path):
    with pytest.raises(BrowserAutomationError):
        _manager(tmp_path, browser_mode="teleport")


def test_cdp_mode_attaches_and_opens_a_new_tab(monkeypatch, tmp_path):
    existing = _FakeContext(existing_pages=3)
    browser = _FakeBrowser([existing])
    _install_fake_playwright(monkeypatch, _FakeChromium())
    monkeypatch.setattr(
        "automation.browser.browser_manager.attach_or_launch_chrome",
        lambda *a, **k: AttachedChrome(
            browser=browser, context=existing, cdp_url="http://127.0.0.1:9222",
            launched_by_us=False, version={"Browser": "Chrome/131"},
        ),
    )

    manager = _manager(tmp_path)
    context = manager.launch_context()
    page = manager.new_page()

    assert context is existing
    assert manager.active_mode == "cdp"
    assert browser.new_context_calls == []       # never an incognito context
    assert existing.new_pages == [page]          # a tab in their window
    # A browser a human is looking at is never "headless", whatever the config
    # says — `should_keep_browser_open()` depends on this being honest.
    assert manager.headless is False


def test_cdp_mode_close_closes_our_tabs_only_and_leaves_chrome_alone(monkeypatch, tmp_path):
    existing = _FakeContext(existing_pages=2)
    their_tabs = list(existing.pages)
    browser = _FakeBrowser([existing])
    playwright = _install_fake_playwright(monkeypatch, _FakeChromium())
    monkeypatch.setattr(
        "automation.browser.browser_manager.attach_or_launch_chrome",
        lambda *a, **k: AttachedChrome(
            browser=browser, context=existing, cdp_url="http://127.0.0.1:9222", launched_by_us=False,
        ),
    )

    manager = _manager(tmp_path)
    our_tab = manager.new_page()
    manager.close()

    assert our_tab.closed is True
    assert all(tab.closed is False for tab in their_tabs)
    assert existing.closed is False   # their context survives
    assert browser.closed is False    # their BROWSER survives — the one unforgivable bug
    assert playwright.stopped is True # but we do let go of the driver


def test_cdp_mode_does_not_export_the_users_whole_cookie_jar(monkeypatch, tmp_path):
    existing = _FakeContext(existing_pages=1)
    browser = _FakeBrowser([existing])
    _install_fake_playwright(monkeypatch, _FakeChromium())
    monkeypatch.setattr(
        "automation.browser.browser_manager.attach_or_launch_chrome",
        lambda *a, **k: AttachedChrome(
            browser=browser, context=existing, cdp_url="http://127.0.0.1:9222", launched_by_us=False,
        ),
    )

    manager = _manager(tmp_path)
    manager.launch_context()
    manager.save_session()

    assert manager.has_saved_session() is False


def test_cdp_failure_falls_back_to_a_persistent_context_not_an_incognito_one(monkeypatch, tmp_path):
    persistent = _FakeContext()
    chromium = _FakeChromium(launched_browser=_FakeBrowser([]))
    _install_fake_playwright(monkeypatch, chromium)

    def _attach_fails(*a, **k):
        raise ChromeAttachError("Chrome is not installed")

    monkeypatch.setattr("automation.browser.browser_manager.attach_or_launch_chrome", _attach_fails)
    captured: dict = {}

    def _persistent(playwright, *, user_data_dir, headless, chrome_path=None):
        captured.update(user_data_dir=str(user_data_dir), headless=headless)
        return persistent

    monkeypatch.setattr("automation.browser.browser_manager.launch_persistent_chrome", _persistent)

    manager = _manager(tmp_path)
    context = manager.launch_context()

    assert context is persistent
    assert manager.active_mode == "persistent"
    assert captured["headless"] is False              # a window a human can take over
    assert "user-1" in captured["user_data_dir"]      # per-user profile, not a shared cookie jar
    assert chromium.launch_calls == []                # and never the old throwaway launch()


def test_persistent_context_is_ours_to_close(monkeypatch, tmp_path):
    persistent = _FakeContext()
    playwright = _install_fake_playwright(monkeypatch, _FakeChromium())
    monkeypatch.setattr(
        "automation.browser.browser_manager.launch_persistent_chrome",
        lambda *a, **k: persistent,
    )

    manager = _manager(tmp_path, browser_mode="persistent")
    manager.launch_context()
    manager.close()

    assert persistent.closed is True
    assert playwright.stopped is True


def test_when_nothing_works_launch_context_raises_and_stops_the_driver(monkeypatch, tmp_path):
    playwright = _install_fake_playwright(monkeypatch, _FakeChromium())

    def _fails(*a, **k):
        raise ChromeAttachError("no browser here")

    monkeypatch.setattr("automation.browser.browser_manager.attach_or_launch_chrome", _fails)
    monkeypatch.setattr("automation.browser.browser_manager.launch_persistent_chrome", _fails)

    manager = _manager(tmp_path)
    with pytest.raises(BrowserAutomationError, match="Could not obtain a browser context"):
        manager.launch_context()
    assert playwright.stopped is True


def test_launch_mode_still_behaves_exactly_as_before(monkeypatch, tmp_path):
    """Regression guard for CI/headless deployments: `launch` mode keeps using
    `new_context(storage_state=...)` from the encrypted SessionStore, and keeps
    honouring AUTOMATION_HEADLESS."""
    browser = _FakeBrowser([])
    _install_fake_playwright(monkeypatch, _FakeChromium(launched_browser=browser))
    monkeypatch.setattr(
        "automation.browser.browser_manager.attach_or_launch_chrome",
        lambda *a, **k: pytest.fail("launch mode must not attach to anything"),
    )

    store = SessionStore(base_dir=tmp_path / "sessions")
    store.save("user-1", "greenhouse", {"cookies": [{"name": "saved"}]})
    manager = BrowserManager(
        user_id="user-1", ats_platform="greenhouse", browser_mode="launch", session_store=store,
    )
    context = manager.launch_context()

    assert manager.active_mode == "launch"
    assert browser.new_context_calls == [{"storage_state": {"cookies": [{"name": "saved"}]}}]

    manager.save_session(context)
    assert manager.has_saved_session() is True

    manager.close()
    assert browser.closed is True


def test_tracing_failure_does_not_take_the_run_down(monkeypatch, tmp_path):
    """An attached browser may refuse instrumentation. Losing the trace is
    acceptable; losing the application run over a debugging aid is not."""
    class _RefusingTracing:
        def start(self, **kwargs):
            raise chrome_attach.PlaywrightError("Tracing is not supported over CDP")

        def stop(self, **kwargs):
            raise AssertionError("stop must not be called for a trace that never started")

    context = _FakeContext()
    context.tracing = _RefusingTracing()
    monkeypatch.setattr("automation.browser.browser_manager.AUTOMATION_LOGS_DIR", str(tmp_path))

    manager = _manager(tmp_path)
    assert manager.start_trace(context) is False
    assert manager.stop_trace("app-1", context) is None
