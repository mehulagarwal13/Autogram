"""
Attaching to the user's real Google Chrome instead of launching a throwaway one.

`BrowserManager` used to do exactly one thing: `chromium.launch()` +
`browser.new_context()`. That combination is a brand-new, empty, *incognito-
equivalent* browser every run — a fresh profile with no cookies, no logged-in
Gmail/LinkedIn/Workday session, and a second window on the user's desktop. This
module is the piece that makes the other two options possible:

1. **Attach** (`connect_to_chrome`) — Playwright's `connect_over_cdp()` speaks
   to a Chrome that is *already running* with `--remote-debugging-port`. The
   returned `Browser.contexts[0]` IS the user's real browsing context: their
   profile, their cookies, their logins. Opening a tab in it is
   `context.new_page()`, which appears as a new tab in their existing window.

2. **Launch-then-attach** (`launch_chrome_with_remote_debugging`) — if nothing
   is listening on the debug port we can start real Chrome ourselves *with* the
   port open, then attach to it. Because that Chrome is a detached process, it
   outlives this Python process, so every later run takes path 1 and opens a
   new tab in the same browser.

Hard constraint on both paths: **we never own that browser.** Nothing in here
closes it, and `BrowserManager.close()` only closes the tabs it opened. Killing
the browser a human is using would be the one unforgivable bug in this file.

`launch_persistent_chrome` is the last-resort fallback for when Chrome can't be
attached to or found at all — a normal (non-incognito) window backed by a real
on-disk profile directory, so cookies and logins still persist across runs.

Nothing here imports from `automation/` — it is a thin, dependency-free layer
over Playwright + the CDP HTTP endpoint, so it can be unit-tested without a
browser.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Playwright,
)

logger = logging.getLogger(__name__)

DEFAULT_CDP_PORT = 9222

#: How long to keep polling Chrome's DevTools HTTP endpoint after we start it.
#: Cold-start Chrome on Windows (profile migration, extension load) is
#: comfortably slower than the ~1s it takes when warm.
_DEVTOOLS_POLL_INTERVAL_S = 0.25


class ChromeAttachError(Exception):
    """Raised when Chrome could not be attached to (or started for attaching).
    Always carries an actionable message — the caller's response is to fall
    back to another browser mode and log this text, so it has to say what the
    user should do about it."""


@dataclass(frozen=True)
class AttachedChrome:
    """The result of a successful attach. `context` is the *existing* default
    context — never one we created — which is what makes `new_page()` a new tab
    in the user's own window."""

    browser: Browser
    context: BrowserContext
    cdp_url: str
    #: True when this process started that Chrome (so it had no windows the
    #: user cares about *yet*). Even then we deliberately leave it running: the
    #: next application run attaches to it instead of starting another one.
    launched_by_us: bool
    version: dict | None = None

    @property
    def browser_label(self) -> str:
        if not self.version:
            return "Chrome (unknown version)"
        return self.version.get("Browser") or "Chrome (unknown version)"


# ---------------------------------------------------------------------------
# CDP endpoint discovery
# ---------------------------------------------------------------------------

def normalize_cdp_url(raw: str | None) -> str:
    """`"9222"`, `"localhost:9222"`, `"http://127.0.0.1:9222/"` -> a canonical
    `http://host:port`. Chrome's DevTools endpoint is HTTP-only and loopback by
    default, so a scheme-less or trailing-slash value from `.env` is a config
    typo worth absorbing rather than failing on."""
    url = (raw or "").strip()
    if not url:
        return f"http://127.0.0.1:{DEFAULT_CDP_PORT}"
    if url.isdigit():
        return f"http://127.0.0.1:{url}"
    if "://" not in url:
        url = f"http://{url}"
    parts = urlsplit(url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or DEFAULT_CDP_PORT
    return f"http://{host}:{port}"


def cdp_port(cdp_url: str) -> int:
    return urlsplit(normalize_cdp_url(cdp_url)).port or DEFAULT_CDP_PORT


def devtools_version(cdp_url: str, *, timeout_s: float = 1.0) -> dict | None:
    """Chrome's `/json/version` payload if something is listening on the debug
    port, else `None`. This is the cheap "is there a Chrome to attach to?"
    probe — a plain HTTP GET, no Playwright driver started, no browser touched.

    `None` covers every flavour of "not attachable" identically (connection
    refused, wrong process on the port, garbage response), because the caller's
    reaction is the same in all of them: try the next mode."""
    url = f"{normalize_cdp_url(cdp_url)}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:  # noqa: S310 - loopback URL we built ourselves
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (OSError, ValueError) as e:  # URLError/timeout/HTTPError/bad JSON
        logger.debug("No CDP endpoint at %s: %s", url, e)
        return None
    return payload if isinstance(payload, dict) else None


def wait_for_devtools(cdp_url: str, *, timeout_s: float) -> dict | None:
    """Polls `devtools_version` until the endpoint answers or `timeout_s`
    elapses. Used only right after we start Chrome ourselves."""
    deadline = time.monotonic() + max(timeout_s, 0.0)
    while True:
        version = devtools_version(cdp_url)
        if version is not None:
            return version
        if time.monotonic() >= deadline:
            return None
        time.sleep(_DEVTOOLS_POLL_INTERVAL_S)


# ---------------------------------------------------------------------------
# Locating Chrome / its profile
# ---------------------------------------------------------------------------

def find_chrome_executable(explicit_path: str | None = None) -> str | None:
    """The real Google Chrome binary (NOT Playwright's bundled Chromium), or
    `None`. `explicit_path` (`AUTOMATION_CHROME_PATH`) always wins so a
    non-standard install location never needs code changes."""
    if explicit_path:
        candidate = Path(explicit_path).expanduser()
        if candidate.exists():
            return str(candidate)
        logger.warning("AUTOMATION_CHROME_PATH=%s does not exist — ignoring it.", explicit_path)

    system = platform.system()
    if system == "Windows":
        roots = [
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        candidates = [
            Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
            for root in roots
            if root
        ]
    elif system == "Darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    else:
        candidates = [Path("/usr/bin/google-chrome"), Path("/usr/bin/google-chrome-stable")]

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    for name in ("chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def default_chrome_user_data_dir() -> Path | None:
    """Where real Chrome keeps the user's actual profile — the one holding
    their Gmail/LinkedIn/Workday logins.

    Only usable as a `--user-data-dir` when Chrome is **completely closed**
    (see `chrome_is_running`): Chrome's ProcessSingleton lock means a second
    process pointed at a live profile hands its command line to the running
    instance and exits — which opens a tab but does NOT open the debug port.
    That's why this is opt-in via `AUTOMATION_CHROME_USER_DATA_DIR=chrome-default`
    rather than the default."""
    system = platform.system()
    if system == "Windows":
        local_appdata = os.environ.get("LOCALAPPDATA")
        path = Path(local_appdata) / "Google" / "Chrome" / "User Data" if local_appdata else None
    elif system == "Darwin":
        path = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    else:
        path = Path.home() / ".config" / "google-chrome"
    return path if path and path.exists() else None


def chrome_is_running() -> bool:
    """Best-effort "is a Chrome process alive right now?". Used purely to turn
    a confusing timeout into a message that names the actual cause, so a
    wrong answer here can only ever downgrade an error message."""
    try:
        if platform.system() == "Windows":
            completed = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            return "chrome.exe" in completed.stdout.lower()
        completed = subprocess.run(
            ["pgrep", "-f", "chrome"], capture_output=True, text=True, timeout=10, check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------------------
# Attach / launch
# ---------------------------------------------------------------------------

def connect_to_chrome(
    playwright: Playwright,
    cdp_url: str,
    *,
    timeout_ms: float = 20_000,
    launched_by_us: bool = False,
    version: dict | None = None,
) -> AttachedChrome:
    """Attaches to an already-running Chrome and returns its **existing**
    default context.

    `browser.contexts[0]` is deliberate and load-bearing:
    `browser.new_context()` over CDP would create a fresh *incognito* context
    inside that same Chrome — a separate cookie jar with none of the user's
    logins, which is the exact problem we're solving. The default context is
    the only one wired to their on-disk profile."""
    endpoint = normalize_cdp_url(cdp_url)
    try:
        browser = playwright.chromium.connect_over_cdp(endpoint, timeout=timeout_ms)
    except PlaywrightError as e:
        raise ChromeAttachError(f"Could not connect to Chrome over CDP at {endpoint}: {e}") from e

    contexts = browser.contexts
    if not contexts:
        # A Chrome with zero browser contexts has no profile-backed window to
        # put a tab in. Disconnect (this does NOT stop that Chrome) and let the
        # caller fall back rather than silently creating an incognito context.
        try:
            browser.close()
        except PlaywrightError:
            pass
        raise ChromeAttachError(
            f"Attached to Chrome at {endpoint} but it exposes no browser context — "
            "open a normal (non-incognito) Chrome window and try again."
        )

    return AttachedChrome(
        browser=browser,
        context=contexts[0],
        cdp_url=endpoint,
        launched_by_us=launched_by_us,
        version=version if version is not None else devtools_version(endpoint),
    )


def launch_chrome_with_remote_debugging(
    *,
    cdp_url: str,
    user_data_dir: str | Path,
    chrome_path: str | None = None,
    timeout_s: float = 30.0,
) -> dict:
    """Starts real Chrome with the debug port open and returns its
    `/json/version` payload once it answers.

    The process is started **detached** on purpose: it must outlive this Python
    process so the browser (and the user's logins in that profile) are still
    there for the next application run, and so a crash on our side never takes
    the user's browser down with it."""
    executable = find_chrome_executable(chrome_path)
    if executable is None:
        raise ChromeAttachError(
            "Google Chrome was not found on this machine. Install Chrome, or set "
            "AUTOMATION_CHROME_PATH in .env to the full path of chrome.exe."
        )

    endpoint = normalize_cdp_url(cdp_url)
    port = cdp_port(endpoint)
    # ABSOLUTE, not just expanded. Chrome resolves a relative --user-data-dir
    # against its own working directory, and when it can't use the directory it
    # falls back to a default profile *without* opening the debug port — a
    # silent failure that presents as a 30s timeout with a healthy-looking
    # Chrome on screen. Our default (`storage/chrome_profile`) is relative, so
    # this is the normal case, not an edge case.
    profile_dir = Path(user_data_dir).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    args = [
        executable,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        # Chrome >=111 refuses DevTools websocket upgrades from unexpected
        # Origins. Playwright sends none, but tooling in front of it might.
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        # Guarantees at least one real window/tab exists the moment the port
        # opens, so `contexts[0]` has somewhere to put our tab.
        "about:blank",
    ]

    popen_kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        popen_kwargs["start_new_session"] = True

    logger.info("Starting Chrome for CDP attach: %s --remote-debugging-port=%d (profile: %s)",
                executable, port, profile_dir)
    try:
        subprocess.Popen(args, **popen_kwargs)  # noqa: S603 - argv list, no shell, path we resolved ourselves
    except OSError as e:
        raise ChromeAttachError(f"Could not start Chrome ({executable}): {e}") from e

    version = wait_for_devtools(endpoint, timeout_s=timeout_s)
    if version is None:
        hint = (
            "Chrome was started but never opened the remote-debugging port within "
            f"{timeout_s:.0f}s."
        )
        if chrome_is_running():
            hint += (
                " Chrome is already running: a second Chrome process pointed at a profile "
                "that is already open just hands its command line to the running instance "
                "and exits, so the debug port never opens. Either quit Chrome completely "
                f"first, or start Chrome yourself with --remote-debugging-port={port} "
                "(see automation/README.md)."
            )
        raise ChromeAttachError(hint)
    return version


def attach_or_launch_chrome(
    playwright: Playwright,
    *,
    cdp_url: str,
    user_data_dir: str | Path,
    autolaunch: bool = True,
    chrome_path: str | None = None,
    timeout_s: float = 30.0,
) -> AttachedChrome:
    """The full preferred path, in order:

    1. Something is already listening on the debug port -> attach to it. This
       is the case that gives the user everything they asked for: their real
       Chrome, their real tabs, their real logins, one new tab.
    2. Nothing listening and `autolaunch` -> start real Chrome with the port
       open, then attach. That Chrome stays running, so from the second run
       onwards we're back in case 1.

    Raises `ChromeAttachError` if neither worked, so `BrowserManager` can fall
    back to a persistent context.
    """
    endpoint = normalize_cdp_url(cdp_url)

    version = devtools_version(endpoint)
    if version is not None:
        logger.info("Found a running Chrome with remote debugging at %s — attaching to it.", endpoint)
        return connect_to_chrome(playwright, endpoint, launched_by_us=False, version=version)

    if not autolaunch:
        raise ChromeAttachError(
            f"Nothing is listening on {endpoint} and AUTOMATION_CDP_AUTOLAUNCH is off. "
            f"Start Chrome with --remote-debugging-port={cdp_port(endpoint)} first."
        )

    version = launch_chrome_with_remote_debugging(
        cdp_url=endpoint, user_data_dir=user_data_dir, chrome_path=chrome_path, timeout_s=timeout_s,
    )
    return connect_to_chrome(playwright, endpoint, launched_by_us=True, version=version)


# ---------------------------------------------------------------------------
# Fallback: persistent (non-incognito) context
# ---------------------------------------------------------------------------

def launch_persistent_chrome(
    playwright: Playwright,
    *,
    user_data_dir: str | Path,
    headless: bool,
    chrome_path: str | None = None,
) -> BrowserContext:
    """A normal browser window backed by a real, reusable on-disk profile.

    This is `launch_persistent_context`, NOT `launch()` + `new_context()`: the
    difference is that cookies, local-storage and logins live in
    `user_data_dir` and are still there on the next run, whereas a
    `new_context()` starts empty every time. Prefers real Chrome
    (`channel="chrome"`) over bundled Chromium so the profile is a genuine
    Chrome profile and sites behave the way they do for the user by hand;
    falls back to Chromium when Chrome isn't installed.
    """
    # Absolute for the same reason as in `launch_chrome_with_remote_debugging`
    # — a browser profile is not a path to be clever about.
    profile_dir = Path(user_data_dir).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    common: dict = {
        "user_data_dir": str(profile_dir),
        "headless": headless,
        "args": ["--no-first-run", "--no-default-browser-check"],
    }
    if not headless:
        # Use the real window size instead of Playwright's 1280x720 emulated
        # viewport — this window is one a human may take over for review.
        common["no_viewport"] = True

    executable = find_chrome_executable(chrome_path)
    attempts: list[tuple[str, dict]] = []
    if chrome_path and executable:
        attempts.append((f"chrome at {executable}", {**common, "executable_path": executable}))
    elif executable:
        attempts.append(("chrome (channel)", {**common, "channel": "chrome"}))
    attempts.append(("bundled chromium", dict(common)))

    last_error: PlaywrightError | None = None
    for label, kwargs in attempts:
        try:
            context = playwright.chromium.launch_persistent_context(**kwargs)
        except PlaywrightError as e:
            last_error = e
            logger.warning("Persistent context via %s failed: %s", label, e)
            continue
        logger.info("Opened a persistent (non-incognito) browser via %s, profile: %s", label, profile_dir)
        return context

    raise ChromeAttachError(
        f"Could not open a persistent browser profile at {profile_dir}: {last_error}"
    )
