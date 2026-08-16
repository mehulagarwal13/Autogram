"""
BrowserManager — Phase 2 (ARCHITECTURE.md).

Owns the Playwright lifecycle: getting a browser, holding one persistent,
encrypted storage-state per (user_id, ats_platform) via `SessionStore`,
capturing screenshots/traces on failure, and retrying transient navigation
errors. Uses Playwright's synchronous API (`playwright.sync_api`) since this
runs from synchronous worker/agent code (Celery/ARQ tasks, Phase 4+), not
inside FastAPI's async request path.

**Getting a browser** is `AUTOMATION_BROWSER_MODE` (see `app/core/config.py`
and `automation/browser/chrome_attach.py`), and the default is no longer "start
a new one":

    cdp (default) -> attach to the user's already-running Chrome over the
                     DevTools Protocol and open a NEW TAB in their existing
                     window/profile. Falls back to `persistent` if that Chrome
                     can't be attached to or started.
    persistent    -> a normal, non-incognito window on a real on-disk profile
                     directory whose cookies survive between runs.
    launch        -> the original behavior: a throwaway browser with an empty,
                     incognito-equivalent context seeded from `SessionStore`.

Only `launch` mode uses `browser.new_context()`, and only `launch` mode is ever
headless. The other two hand back a context whose cookies belong to a real
browser profile, which is what makes an already-logged-in
LinkedIn/Workday/Greenhouse session usable instead of being re-authenticated
(or hitting a login wall) on every single run.

**Ownership** is the other half of the contract. In `cdp` mode the browser is
the user's, not ours: `close()` closes only the tabs this manager opened and
then drops the driver connection. It never closes that browser — including one
we started ourselves on their behalf, which is deliberately left running so the
next application lands as another tab in the same session.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from app.core.config import (
    AUTOMATION_BROWSER_MODE,
    AUTOMATION_CDP_AUTOLAUNCH,
    AUTOMATION_CDP_LAUNCH_TIMEOUT_S,
    AUTOMATION_CDP_URL,
    AUTOMATION_CHROME_PATH,
    AUTOMATION_CHROME_USER_DATA_DIR,
    AUTOMATION_HEADLESS,
    AUTOMATION_LOGS_DIR,
)
from automation.browser.chrome_attach import (
    ChromeAttachError,
    attach_or_launch_chrome,
    default_chrome_user_data_dir,
    launch_persistent_chrome,
)
from automation.browser.session import SessionStore

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Transient Playwright errors worth retrying (navigation timeouts, detached
# frames, target-closed races) as opposed to logic errors in our own code.
_RETRYABLE_ERRORS: tuple[type[Exception], ...] = (PlaywrightTimeoutError, PlaywrightError)

#: Which modes each configured mode may fall back to, in order. Note what is
#: NOT here: nothing falls back to "launch". Once a deployment has asked for a
#: real browser profile, silently downgrading to an empty incognito context
#: would take away every login the user expects to still be signed into — a
#: failure that looks like "the ATS logged me out again", not like a
#: misconfiguration. `persistent` needs nothing but a writable directory, so it
#: is a sufficient last resort.
_MODE_CHAIN: dict[str, tuple[str, ...]] = {
    "cdp": ("cdp", "persistent"),
    "persistent": ("persistent",),
    "launch": ("launch",),
}


class BrowserAutomationError(Exception):
    """Raised when a browser action fails after all retries, or when a
    BrowserManager method is used out of sequence (e.g. saving a session
    with no active context)."""


class BrowserManager:
    """Attaches to (or, as a fallback, opens) a browser context for one
    (user_id, ats_platform) pair, and provides the cross-cutting operations
    every ATS adapter (Phase 3/4) needs: session persistence, retries,
    screenshots, and traces.

    One instance = one browser/context lifecycle. Typical use:

        manager = BrowserManager(user_id=user.user_id, ats_platform="greenhouse")
        with manager.session() as context:
            page = manager.new_page()      # a NEW TAB in the user's Chrome
            manager.run_with_retries(lambda: page.goto(job_url))
            ...
    """

    def __init__(
        self,
        user_id: str,
        ats_platform: str,
        headless: bool | None = None,
        session_store: SessionStore | None = None,
        browser_mode: str | None = None,
        cdp_url: str | None = None,
    ) -> None:
        self.user_id = user_id
        self.ats_platform = ats_platform
        self.browser_mode = (browser_mode or AUTOMATION_BROWSER_MODE).strip().lower()
        if self.browser_mode not in _MODE_CHAIN:
            raise BrowserAutomationError(
                f"Unknown browser_mode {self.browser_mode!r} — expected one of {sorted(_MODE_CHAIN)}."
            )
        self.cdp_url = cdp_url or AUTOMATION_CDP_URL
        # `AUTOMATION_HEADLESS` only means anything for a browser we own and
        # nobody watches. Attaching to a human's Chrome cannot be headless, and
        # the persistent fallback exists precisely so a human can watch/take
        # over — so an unspecified `headless` is False in both of those modes
        # regardless of the config default.
        if headless is None:
            headless = AUTOMATION_HEADLESS if self.browser_mode == "launch" else False
        self.headless = headless
        self._session_store = session_store or SessionStore()

        self._playwright = None
        self._browser = None
        self._context: BrowserContext | None = None
        #: The mode that actually produced `_context` (may differ from
        #: `browser_mode` after a fallback). `None` until a context exists.
        self.active_mode: str | None = None
        #: False whenever the browser belongs to the user rather than to us —
        #: see `close()`. Assume ownership until an attach proves otherwise.
        self._owns_browser = True
        #: Tabs this manager opened, so `close()` can clean up exactly those and
        #: nothing else when the browser isn't ours to close.
        self._pages: list[Page] = []
        self._tracing_started = False

    # ------------------------------------------------------------------
    # Lifecycle: attach / launch / reuse / close
    # ------------------------------------------------------------------

    def launch_context(self) -> BrowserContext:
        """Returns the browser context this run works in, per
        `AUTOMATION_BROWSER_MODE` (attaching to the user's running Chrome by
        default — see the module docstring). Idempotent per instance: a second
        call reuses the already-open context rather than getting a second
        browser."""
        if self._context is not None:
            return self._context

        self._playwright = sync_playwright().start()

        failures: list[str] = []
        for mode in _MODE_CHAIN[self.browser_mode]:
            try:
                context = self._open_context(mode)
            except (ChromeAttachError, PlaywrightError, OSError) as e:
                failures.append(f"{mode}: {e}")
                logger.warning("Browser mode '%s' unavailable — %s", mode, e)
                continue

            self._context = context
            self.active_mode = mode
            logger.info(
                "Browser ready (mode=%s, headless=%s, ours_to_close=%s).",
                mode, self.headless, self._owns_browser,
            )
            return context

        # Nothing worked: don't leave the driver process we just started behind.
        self.close()
        raise BrowserAutomationError(
            "Could not obtain a browser context. Tried: " + " | ".join(failures)
        )

    def _open_context(self, mode: str) -> BrowserContext:
        if mode == "cdp":
            return self._attach_over_cdp()
        if mode == "persistent":
            return self._open_persistent_context()
        return self._launch_throwaway_context()

    def _attach_over_cdp(self) -> BrowserContext:
        """Attaches to the user's Chrome and returns its EXISTING default
        context — the one holding their profile's cookies and logins. Tabs
        opened here appear in the window they already have on screen."""
        attached = attach_or_launch_chrome(
            self._playwright,
            cdp_url=self.cdp_url,
            user_data_dir=self._profile_dir("cdp"),
            autolaunch=AUTOMATION_CDP_AUTOLAUNCH,
            chrome_path=AUTOMATION_CHROME_PATH,
            timeout_s=AUTOMATION_CDP_LAUNCH_TIMEOUT_S,
        )
        self._browser = attached.browser
        self._owns_browser = False
        # A real, on-screen Chrome. Reporting this accurately isn't cosmetic:
        # `should_keep_browser_open()` uses `headless` to decide whether there's
        # anything for a human to review, and a copilot run whose tab got closed
        # because we still claimed to be headless would be exactly the handoff
        # this feature exists to provide.
        self.headless = False
        logger.info(
            "Attached to %s over CDP at %s (%d existing tab(s)); this browser will NOT be closed by us.",
            attached.browser_label, attached.cdp_url, len(attached.context.pages),
        )
        return attached.context

    def _open_persistent_context(self) -> BrowserContext:
        """Fallback: our own normal (non-incognito) window on a real profile
        directory. Not the user's live Chrome — but not a blank slate either,
        since the profile persists, so a login done once here is still there on
        the next run."""
        context = launch_persistent_chrome(
            self._playwright,
            user_data_dir=self._profile_dir("persistent"),
            headless=self.headless,
            chrome_path=AUTOMATION_CHROME_PATH,
        )
        # A persistent context owns its browser process: closing the context
        # closes the browser, so there is no separate Browser to close.
        self._browser = None
        self._owns_browser = True
        return context

    def _launch_throwaway_context(self) -> BrowserContext:
        """The original Phase 2 path, kept for CI and headless servers: a fresh
        browser plus an empty context seeded from the encrypted storage-state
        for this (user, ATS), if any."""
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        storage_state = self._session_store.load(self.user_id, self.ats_platform)
        self._owns_browser = True
        return (
            self._browser.new_context(storage_state=storage_state)
            if storage_state
            else self._browser.new_context()
        )

    def _profile_dir(self, mode: str) -> Path:
        """The browser profile directory for a browser we start ourselves.

        Per-(user, mode) rather than one shared directory, for two reasons:
        this is a multi-user application and one profile per deployment would
        pool every user's cookies into a single jar; and a `cdp`-launched Chrome
        left running holds an exclusive lock on its profile, which would then
        block the `persistent` fallback from opening the same directory."""
        if AUTOMATION_CHROME_USER_DATA_DIR.strip().lower() == "chrome-default":
            real_profile = default_chrome_user_data_dir()
            if real_profile is not None:
                # Explicitly opted in to the user's own Chrome profile: use it
                # as-is (no per-user subdirectory — it IS their profile).
                return real_profile
            logger.warning(
                "AUTOMATION_CHROME_USER_DATA_DIR=chrome-default but no Chrome profile was found "
                "for this OS — falling back to a dedicated Autogram profile."
            )
            base = Path("storage/chrome_profile")
        else:
            base = Path(AUTOMATION_CHROME_USER_DATA_DIR)
        safe_user = self.user_id.replace("/", "_").replace("\\", "_")
        return base / mode / safe_user

    def new_page(self) -> Page:
        """Opens a fresh page in this run's context — a new tab in the user's
        existing Chrome window under the default `cdp` mode. Tracked, so
        `close()` can clean up our tabs without touching theirs."""
        page = self.launch_context().new_page()
        self._pages.append(page)
        return page

    def adopt_page(self, page: Page) -> None:
        """Registers a page this run didn't open via `new_page()` itself —
        e.g. a new tab a job posting's own "Apply" link opened
        (`target="_blank"`), which `ApplicationFlowManager` picks up via
        `context.expect_page()` rather than calling `new_page()` — so `close()`
        cleans it up exactly like any tab we opened directly. A no-op for
        cleanup purposes if `page` is somehow already tracked."""
        if page not in self._pages:
            self._pages.append(page)

    def has_saved_session(self) -> bool:
        return self._session_store.has_session(self.user_id, self.ats_platform)

    def save_session(self, context: BrowserContext | None = None) -> None:
        """Persists the context's current storage-state (cookies/local-storage),
        encrypted, for reuse on the next run (see `SessionStore`).

        A no-op when the context is backed by a real browser profile (`cdp`,
        `persistent`): that profile already persists cookies far better than we
        can, and in `cdp` mode `storage_state()` would copy the user's ENTIRE
        Chrome cookie jar — every site, not just this ATS — into our storage.
        Not needed, and not ours to take."""
        context = context or self._context
        if context is None:
            raise BrowserAutomationError("No active browser context to save a session from.")
        if self.active_mode in ("cdp", "persistent"):
            logger.debug(
                "Not exporting storage-state in '%s' mode — the browser profile is the source of truth.",
                self.active_mode,
            )
            return
        self._session_store.save(self.user_id, self.ats_platform, context.storage_state())

    def close(self) -> None:
        """Releases this run's browser resources.

        When the browser is ours (`persistent`/`launch`) this closes the
        context, the browser, and the driver, in order. When it is NOT ours
        (`cdp` — the user's own Chrome, or a Chrome we started for them that is
        meant to outlive this run) it closes only the tabs we opened and then
        drops the driver connection; the browser, its other tabs, and its
        logins are left exactly as they were. Note in particular that
        `browser.close()` is never called on an attached browser — over CDP that
        disconnects, but it is one API change away from taking a human's whole
        browser down, so it simply isn't in this path.

        Best-effort throughout: an error closing an already-closed resource is
        logged, not raised, so cleanup after a crash never masks the original
        error."""
        closers: list[Callable[[], None]] = []
        if self._owns_browser:
            closers += [
                lambda: self._context.close() if self._context else None,
                lambda: self._browser.close() if self._browser else None,
            ]
        else:
            closers.append(self._close_our_pages)
        closers.append(lambda: self._playwright.stop() if self._playwright else None)

        for closer in closers:
            try:
                closer()
            except PlaywrightError as e:
                logger.warning("Error during browser cleanup (ignored): %s", e)

        self._pages.clear()
        self._context = None
        self._browser = None
        self._playwright = None
        self.active_mode = None
        self._tracing_started = False
        self._owns_browser = True

    def _close_our_pages(self) -> None:
        for page in self._pages:
            try:
                if not page.is_closed():
                    page.close()
            except PlaywrightError as e:
                logger.debug("Could not close a tab we opened (ignored): %s", e)

    @contextmanager
    def session(self, *, persist: bool = True) -> Iterator[BrowserContext]:
        """Convenience context manager for ATS adapters:

            with browser_manager.session() as context:
                page = context.new_page()
                ...

        Saves the (possibly newly-authenticated) session on clean exit unless
        `persist=False`, and always releases the browser — even on error. In
        `cdp` mode "releases" means our tabs only; see `close()`."""
        context = self.launch_context()
        try:
            yield context
            if persist:
                self.save_session(context)
        finally:
            self.close()

    # ------------------------------------------------------------------
    # Authentication: manual-login primitive (Phase 7 builds the user-facing
    # handoff; see ARCHITECTURE.md "No password harvesting" — only the
    # resulting cookies/local-storage are ever persisted, never a password)
    # ------------------------------------------------------------------

    @contextmanager
    def manual_login_session(self, url: str) -> Iterator[Page]:
        """Opens a NON-headless tab (regardless of `self.headless`) navigated to
        `url`, for the user to log in by hand. On clean exit, the resulting
        session is saved via `SessionStore` — except in the profile-backed modes
        where the browser keeps it itself (see `save_session`). This method only
        provides the underlying Playwright primitive — the actual UI handoff
        to the user (e.g. a "watch and take over" flow) is Phase 7 work.

        Goes through the same mode chain as everything else, which is a real
        improvement here specifically: under `cdp` the "log in by hand" tab
        opens in the browser the user is already sitting in front of, where
        they may well be signed in already and have their password manager."""
        if self._context is not None:
            raise BrowserAutomationError("A context is already active on this BrowserManager instance.")

        previous_headless = self.headless
        self.headless = False  # a login only a human can do must be visible
        try:
            context = self.launch_context()
            page = self.new_page()
            try:
                page.goto(url)
                yield page
                self.save_session(context)
            finally:
                self.close()
        finally:
            self.headless = previous_headless

    # ------------------------------------------------------------------
    # Retries
    # ------------------------------------------------------------------

    def run_with_retries(
        self,
        action: Callable[[], T],
        *,
        max_attempts: int = 3,
        backoff_base_seconds: float = 2.0,
        retry_on: tuple[type[Exception], ...] = _RETRYABLE_ERRORS,
    ) -> T:
        """Runs `action()` (a zero-arg callable, typically a small lambda
        wrapping one Playwright call), retrying on `retry_on` exceptions with
        exponential backoff. Mirrors the retry shape of
        `app/ai/llm/router.py::LLMRouter.run` elsewhere in this codebase, so
        the two "flaky external call" spots in the app behave consistently."""
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return action()
            except retry_on as e:
                last_error = e
                if attempt < max_attempts:
                    delay = backoff_base_seconds * (2 ** (attempt - 1))
                    logger.warning(
                        "Browser action attempt %d/%d failed: %s — retrying in %.1fs",
                        attempt, max_attempts, e, delay,
                    )
                    time.sleep(delay)

        raise BrowserAutomationError(
            f"Browser action failed after {max_attempts} attempts: {last_error}"
        ) from last_error

    # ------------------------------------------------------------------
    # Screenshots / traces / error log (§14 Logging and Debugging)
    # ------------------------------------------------------------------

    def _run_dir(self, application_id: str) -> Path:
        directory = Path(AUTOMATION_LOGS_DIR) / application_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def run_directory(self, application_id: str) -> Path:
        """This run's artifact directory (`logs/<application_id>/`), created if
        needed — the same one screenshots, the trace, and the error log go to.
        Public so a caller with its own artifact to write can put it alongside
        them instead of inventing a second location: the vision fallback pass
        saves the cropped screenshots it sent to the model here, which is the
        first thing anyone reviewing a questionable vision answer wants to
        see."""
        return self._run_dir(application_id)

    def screenshot_on_failure(self, page: Page, application_id: str) -> str:
        """Saves a full-page screenshot to `logs/<application_id>/screenshotN.png`
        (N auto-incremented) and returns its path — feeds
        `automation_runs.screenshot_paths`."""
        directory = self._run_dir(application_id)
        next_index = len(list(directory.glob("screenshot*.png"))) + 1
        path = directory / f"screenshot{next_index}.png"
        page.screenshot(path=str(path), full_page=True)
        return str(path)

    def start_trace(self, context: BrowserContext | None = None) -> bool:
        """Starts a Playwright trace, returning whether it actually started.

        Tracing is a debugging aid, not part of the application: on a context we
        attached to rather than created, Playwright may refuse to instrument it
        (it doesn't own the browser's launch arguments). Losing the trace is an
        acceptable cost of running in the user's own Chrome — losing the whole
        application run over it is not, so this reports failure instead of
        raising. Still raises for a genuine programming error (no context at
        all), which no browser mode can cause."""
        context = context or self._context
        if context is None:
            raise BrowserAutomationError("No active browser context to trace.")
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
        except PlaywrightError as e:
            logger.warning(
                "Tracing is unavailable in '%s' browser mode — continuing without a trace: %s",
                self.active_mode, e,
            )
            return False
        self._tracing_started = True
        return True

    def stop_trace(self, application_id: str, context: BrowserContext | None = None) -> str | None:
        """Stops tracing and writes `logs/<application_id>/trace.zip`
        (viewable at https://trace.playwright.dev) — feeds
        `automation_runs.trace_path`. `None` when tracing never started (see
        `start_trace`), since there is no trace file to point at."""
        context = context or self._context
        if context is None:
            raise BrowserAutomationError("No active browser context to trace.")
        if not self._tracing_started:
            return None
        path = self._run_dir(application_id) / "trace.zip"
        context.tracing.stop(path=str(path))
        self._tracing_started = False
        return str(path)

    def write_error_log(self, application_id: str, message: str) -> str:
        """Appends `message` to `logs/<application_id>/error.log` and returns
        its path — feeds `automation_runs.error_log`."""
        path = self._run_dir(application_id) / "error.log"
        with open(path, "a", encoding="utf-8") as f:
            f.write(message.rstrip("\n") + "\n")
        return str(path)
