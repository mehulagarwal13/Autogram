"""
A minimal, self-contained LOCAL test site for end-to-end Human-in-the-Loop
browser validation. Started in-process on an ephemeral port by
`automation/tests/test_e2e_hitl_browser.py`; serves a handful of pages that
reproduce each blocker the autonomous agent must detect and pause on.

**Why a local fixture rather than a real career site:** the whole point is to
assert on exact state transitions (was `detect_blocker` reached before the
LLM? was the code typed into THAT field? did the page change after submit?),
which requires a page whose markup and accept/reject logic we control
precisely. It also keeps the suite hermetic — no network, no rate limits, no
real site's Terms of Service.

**Deliberately NOT here:** any real CAPTCHA service integration, or anything
resembling a CAPTCHA bypass. `/captcha` renders static text plus a button a
human clicks, which is exactly the "human completes the challenge in the
browser" handoff the product requires — the automation never solves it.

Pages (all under the server's base URL):

    /apply                normal application form (no blocker)
    /otp                  OTP challenge: <input autocomplete="one-time-code">
    /otp?mode=vanish      OTP field is removed 1s after load (field-disappears test)
    /mfa                  two-factor / authenticator-app challenge
    /login                login wall with a <input type="password">
    /captcha              simulated anti-bot challenge (human clicks to clear)
    /upload               a form whose only required field is a résumé file input
    /manual               a manual-action-required page
    /confirmed            the post-submit confirmation page
    /next-step            the page reached after a correct OTP

The valid OTP is `VALID_OTP`; anything else is rejected the way a real site
would reject it (an error message, the same field still present), which is
what drives the "rejected code raises a brand-new request" assertion.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

#: The one code the fixture accepts. Distinctive so a substring search across
#: DB rows / logs / API responses can't false-negative.
VALID_OTP = "424242"
#: Used by tests that must submit a *wrong* code.
INVALID_OTP = "111111"

_HEAD = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title></head><body>
<h1>{heading}</h1>"""
_FOOT = "</body></html>"


def _page(title: str, heading: str, body: str) -> str:
    return _HEAD.format(title=title, heading=heading) + body + _FOOT


# --- Normal application form: nothing here should trigger a blocker --------
APPLY_PAGE = _page(
    "Apply - Acme Careers", "Software Engineer Application",
    """
    <form method="POST" action="/apply">
      <label for="first">First name</label>
      <input id="first" name="first" type="text" autocomplete="given-name">
      <label for="last">Last name</label>
      <input id="last" name="last" type="text" autocomplete="family-name">
      <label for="email">Email</label>
      <input id="email" name="email" type="email" autocomplete="email">
      <label for="years">Years of experience</label>
      <input id="years" name="years" type="text">
      <button type="submit" id="next-btn">Save and continue</button>
    </form>
    """,
)

# --- OTP challenge --------------------------------------------------------
# `autocomplete="one-time-code"` is the single strongest Layer-1 signal
# (`observer.py::detect_blocker`), plus verification-code prose for Layer 2
# and a masked destination for the modal's courtesy line.
def _otp_page(error: str = "", vanish: bool = False) -> str:
    error_html = f'<p id="otp-error" style="color:red">{error}</p>' if error else ""
    vanish_script = (
        """<script>
             setTimeout(function () {
               var f = document.getElementById('code');
               if (f) { f.parentNode.removeChild(f); }
               document.getElementById('gone').textContent = 'This step is no longer available.';
             }, 800);
           </script>"""
        if vanish else ""
    )
    return _page(
        "Verify - Acme Careers", "Verify your identity",
        f"""
        <p>We sent a verification code to j***@gmail.com. Enter the code to continue.</p>
        {error_html}
        <form method="POST" action="/otp">
          <label for="code">Verification code</label>
          <input id="code" name="code" type="text" autocomplete="one-time-code"
                 inputmode="numeric" maxlength="6">
          <button type="submit" id="verify-btn">Verify</button>
        </form>
        <p id="gone"></p>
        {vanish_script}
        """,
    )


def _otp_split_page(error: str = "") -> str:
    """OTP as SIX single-character boxes — the layout American Express uses.

    The important detail is `maxlength="1"` on each box: filling the first with
    the whole code stores one character, which is exactly the bug this fixture
    exists to catch. The submit handler joins the six values, so a run that
    only populated box one fails here the same way it fails on the real site.
    """
    error_html = f'<p id="otp-error" style="color:red">{error}</p>' if error else ""
    boxes = "".join(
        f'<input id="d{i}" name="code{i}" type="text" autocomplete="one-time-code" '
        f'inputmode="numeric" maxlength="1" style="width:2em">'
        for i in range(6)
    )
    return _page(
        "Confirm Your Identity - Acme Careers", "Confirm Your Identity",
        f"""
        <p>The verification code was sent to j***@gmail.com.</p>
        {error_html}
        <form method="POST" action="/otp-split">
          {boxes}
          <button type="submit" id="verify-btn">Verify</button>
        </form>
        """,
    )


def _blocked_page(filled: bool = False) -> str:
    """A page that CANNOT be advanced until a required field is filled.

    Models the capability gap rather than a security gate: there is no CAPTCHA
    and no OTP here, just a required input the automation could not supply and
    an "Add Experience" repeating section it cannot drive — the shape that made
    a real American Express run stall silently.
    """
    value = 'value="Filled by a human"' if filled else ""
    return _page(
        "Education and Experience - Acme Careers", "Education and Experience",
        f"""
        <p>Add your experience.</p>
        <button type="button" id="add-experience">Add Experience</button>
        <label for="req">Years in role</label>
        <input id="req" name="years_in_role" type="text" required {value}>
        <form method="POST" action="/blocked"><button type="submit" id="next-btn">Next</button></form>
        """,
    )


# --- MFA / authenticator-app challenge -----------------------------------
MFA_PAGE = _page(
    "Two-factor authentication", "Two-factor authentication required",
    """
    <p>Open your authenticator app and enter the 6-digit code.</p>
    <form method="POST" action="/mfa">
      <label for="mfacode">Authenticator code</label>
      <input id="mfacode" name="mfacode" type="text" autocomplete="one-time-code" maxlength="6">
      <button type="submit">Verify</button>
    </form>
    """,
)

# --- Login wall -----------------------------------------------------------
# A real `type="password"` field: the Layer-1 signal that must produce
# LOGIN_REQUIRED, and which the observer must never read a value from.
LOGIN_PAGE = _page(
    "Sign in - Acme Careers", "Please sign in to continue",
    """
    <form method="POST" action="/login">
      <label for="user">Email</label>
      <input id="user" name="user" type="email" autocomplete="username">
      <label for="pw">Password</label>
      <input id="pw" name="pw" type="password" autocomplete="current-password">
      <button type="submit">Sign in</button>
    </form>
    """,
)

# --- Simulated anti-bot challenge ---------------------------------------
# NOT a real CAPTCHA and NOT a bypass of one: static text plus a button a
# human clicks. Mirrors the product rule — pause, hand off to the human,
# resume only after the page state actually changes.
# Clicking through NAVIGATES to the ordinary application form, which is what
# a real anti-bot gate does once it is satisfied. That matters for the test:
# `observer.py::detect_blocker`'s Layer-2 text matching is deliberately
# conservative and matches "security check", so a page that cleared its
# challenge *in place* while keeping that heading would still read as
# CAPTCHA_REQUIRED and the agent would (correctly, if unhelpfully) pause
# again. Navigating away is both the realistic behavior and the one that lets
# this test assert the agent genuinely resumes. See AUTONOMOUS_AGENT.md's
# known-limitations note on in-place CAPTCHA clearing.
CAPTCHA_PAGE = _page(
    "Security check", "Security check",
    """
    <p>Please verify you are human before continuing.</p>
    <div id="challenge">
      <button id="captcha-btn" onclick="
        document.getElementById('challenge').remove();
        document.getElementById('cleared').textContent = 'Verification complete. Continuing...';
        setTimeout(function () { window.location = '/apply'; }, 150);
      ">I am not a robot</button>
    </div>
    <p id="cleared"></p>
    """,
)

# The file input essentially every real job application has, and which the
# original fixture lacked entirely — which is why an always-empty
# `uploaded_documents` (so: the agent could never attach a résumé) survived a
# 14/14 end-to-end pass. Deliberately carries a resume/CV-hinted label so the
# observer reports it as such.
UPLOAD_PAGE = _page(
    "Attach your resume - Acme Careers", "Attach your resume",
    """
    <form method="POST" action="/upload" enctype="multipart/form-data">
      <label for="resume">Resume/CV</label>
      <input id="resume" name="resume" type="file" accept=".pdf,.docx">
      <button type="submit" id="upload-btn">Continue</button>
    </form>
    """,
)

MANUAL_PAGE = _page(
    "Additional step required", "Additional step required",
    """
    <p>This application requires an additional step that must be completed manually
       in your browser before continuing.</p>
    <button id="manual-btn">Acknowledge</button>
    """,
)

NEXT_STEP_PAGE = _page(
    "Work history - Acme Careers", "Work history",
    """
    <p>Verification complete. Please add your work history.</p>
    <form method="POST" action="/confirmed">
      <label for="company">Most recent company</label>
      <input id="company" name="company" type="text">
      <button type="submit" id="finish-btn">Save and continue</button>
    </form>
    """,
)

CONFIRMED_PAGE = _page(
    "Application submitted", "Application submitted",
    "<p>Thank you for applying. We have received your application.</p>",
)


class _Handler(BaseHTTPRequestHandler):
    # Silence the default per-request stderr logging — it would flood pytest
    # output, and the tests assert on OUR logs, not the fixture's.
    def log_message(self, fmt, *args):
        pass

    def handle_one_request(self):
        """Chrome opens speculative keep-alive sockets and resets them without
        sending a request, which the stdlib handler surfaces as a full
        `ConnectionResetError` traceback on stderr. Harmless, but it buries
        real pytest failure output — so swallow exactly that class of error."""
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True

    def handle_error(self, *args, **kwargs):  # pragma: no cover - defensive
        pass

    def _send(self, html: str, status: int = 200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # No caching: several tests re-visit the same path expecting fresh
        # server-side state (e.g. an OTP error message).
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str):
        """303 See Other — what a real site does after a successful POST, and
        what makes the browser's URL actually CHANGE. Without this the tab
        would still read `/otp` after a successful verification (the response
        body would be the next page but the address wouldn't move), which is
        both unrealistic and untestable."""
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path

        if path in ("/", "/apply"):
            self._send(APPLY_PAGE)
        elif path == "/blocked":
            self._send(_blocked_page(query.get("filled", [""])[0] == "1"))
        elif path == "/otp-split":
            self._send(_otp_split_page())
        elif path == "/otp":
            self._send(_otp_page(vanish=query.get("mode", [""])[0] == "vanish"))
        elif path == "/mfa":
            self._send(MFA_PAGE)
        elif path == "/login":
            self._send(LOGIN_PAGE)
        elif path == "/captcha":
            self._send(CAPTCHA_PAGE)
        elif path == "/upload":
            self._send(UPLOAD_PAGE)
        elif path == "/manual":
            self._send(MANUAL_PAGE)
        elif path == "/next-step":
            self._send(NEXT_STEP_PAGE)
        elif path == "/confirmed":
            self._send(CONFIRMED_PAGE)
        else:
            self._send(_page("Not found", "Not found", "<p>No such page.</p>"), status=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        form = parse_qs(raw)

        if parsed.path == "/otp-split":
            # Join the six per-digit boxes the way the real component does.
            joined = "".join((form.get(f"code{i}") or [""])[0] for i in range(6))
            if joined == VALID_OTP:
                self._redirect("/next-step")
            else:
                self._send(_otp_split_page(error="That code was not accepted or has expired."))
        elif parsed.path in ("/otp", "/mfa"):
            submitted = (form.get("code") or form.get("mfacode") or [""])[0]
            if submitted == VALID_OTP:
                # Accepted: redirect onward, so the browser's URL really moves
                # and the OTP field is genuinely GONE from the new page.
                self._redirect("/next-step")
            else:
                # Rejected the way a real site does: same field still present,
                # plus an error. This is what must produce a brand-new
                # HumanInteractionRequest rather than an automated retry.
                self._send(_otp_page(error="That code was not accepted or has expired."))
        elif parsed.path in ("/apply", "/upload"):
            self._redirect("/next-step")
        elif parsed.path == "/login":
            self._redirect("/apply")  # "logged in" -> on to the ordinary form
        elif parsed.path == "/confirmed":
            self._redirect("/confirmed")
        else:
            self._send(_page("Not found", "Not found", "<p>No such page.</p>"), status=404)


class HitlTestSite:
    """Runs `_Handler` on an ephemeral localhost port in a daemon thread.

        with HitlTestSite() as site:
            page.goto(site.url("/otp"))
    """

    def __init__(self) -> None:
        # Port 0 -> the OS assigns a free port, so parallel/repeat runs never
        # collide on a hardcoded one. Threading, because a real browser opens
        # several concurrent connections per page load and a single-threaded
        # server would serialize (and occasionally stall) them.
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def url(self, path: str = "/apply") -> str:
        return f"{self.base_url}{path}"

    def start(self) -> "HitlTestSite":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "HitlTestSite":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
