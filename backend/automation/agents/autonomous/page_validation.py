"""Generic post-navigation error-page detection.

This deliberately uses browser-level signals only (HTTP status when available,
URL, title, headings and short visible text). It has no ATS or employer rules.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlsplit


_ERROR_STATUS_CODES = frozenset({400, 401, 403, 404, 405, 408, 409, 410, 429, 500, 501, 502, 503, 504})
_ERROR_PATH_RE = re.compile(r"(?:^|/)(?:404|403|500|error|not-found|access-denied)(?:/|$)", re.IGNORECASE)
_STRONG_ERROR_TEXT_RE = re.compile(
    r"^(?:404|403|500)(?:\s|$)|"
    r"page\s+not\s+found|the\s+page\s+(?:you\s+requested\s+)?(?:was|could\s+not\s+be)\s+found|"
    r"access\s+denied|permission\s+denied|forbidden|internal\s+server\s+error|"
    r"service\s+unavailable|something\s+went\s+wrong|an\s+unexpected\s+error\s+occurred",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PageError:
    code: str
    detail: str
    status: int | None = None


def detect_error_page(page, *, response_status: int | None = None) -> PageError | None:
    if response_status is not None and (response_status in _ERROR_STATUS_CODES or response_status >= 400):
        return PageError("HTTP_ERROR", f"destination returned HTTP {response_status}", response_status)

    try:
        current_url = page.url or ""
    except Exception:  # noqa: BLE001 - validation must degrade conservatively
        current_url = ""
    try:
        path = urlsplit(current_url).path
    except ValueError:
        path = ""
    if _ERROR_PATH_RE.search(path):
        return PageError("ERROR_PAGE", f"destination URL identifies an error page: {path}")

    try:
        title = " ".join((page.title() or "").split())
    except Exception:  # noqa: BLE001
        title = ""
    if title and _STRONG_ERROR_TEXT_RE.search(title):
        return PageError("ERROR_PAGE", f"destination title identifies an error page: {title[:160]}")

    # Restrict content matching to the beginning of the page. Job descriptions
    # can mention error handling/access permissions later in their text; a real
    # error page presents its message as the primary content.
    try:
        body_start = " ".join((page.inner_text("body") or "").split())[:700]
    except Exception:  # noqa: BLE001
        body_start = ""
    if body_start and _STRONG_ERROR_TEXT_RE.search(body_start):
        return PageError("ERROR_PAGE", f"destination content identifies an error page: {body_start[:180]}")
    return None
