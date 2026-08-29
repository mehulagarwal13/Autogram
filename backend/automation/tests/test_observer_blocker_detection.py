"""
Unit tests for `automation/agents/autonomous/observer.py::detect_blocker` —
the deterministic (non-LLM) Layers 1/2 human-blocker detector. Pure function
over plain dicts/strings, so no Playwright/DB/LLM is needed here.
"""

from automation.agents.autonomous.observer import detect_blocker


def _el(ref, tag="input", type_="text", name="", **extra):
    return {"ref": ref, "tag": tag, "type": type_, "name": name, **extra}


def test_password_field_is_login_required():
    elements = [_el(0, type_="password", name="Password")]
    blocker = detect_blocker("https://x.com/apply", "Create your account", elements)
    assert blocker["request_type"] == "LOGIN_REQUIRED"
    assert blocker["otp_field_ref"] is None


def test_otp_field_via_autocomplete_attribute():
    elements = [_el(2, name="Code", autocomplete="one-time-code")]
    blocker = detect_blocker("https://x.com/verify", "Enter the 6-digit code we sent you", elements)
    assert blocker["request_type"] == "OTP_REQUIRED"
    assert blocker["otp_field_ref"] == 2


def test_otp_field_via_name_pattern_without_autocomplete():
    elements = [_el(3, name="One-time verification code")]
    blocker = detect_blocker("https://x.com/verify", "", elements)
    assert blocker["request_type"] == "OTP_REQUIRED"
    assert blocker["otp_field_ref"] == 3


def test_otp_field_via_numeric_short_maxlength():
    elements = [_el(4, name="code", inputmode="numeric", maxlength=6)]
    blocker = detect_blocker("https://x.com/verify", "", elements)
    assert blocker["request_type"] == "OTP_REQUIRED"
    assert blocker["otp_field_ref"] == 4


def test_otp_text_without_a_matching_field_still_detected():
    blocker = detect_blocker("https://x.com/verify", "Please enter the verification code sent to your email", [])
    assert blocker["request_type"] == "OTP_REQUIRED"
    assert blocker["otp_field_ref"] is None


def test_mfa_text_is_classified_distinctly_from_plain_otp():
    blocker = detect_blocker("https://x.com/verify", "Two-factor authentication is required to continue", [])
    assert blocker["request_type"] == "MFA_REQUIRED"


def test_captcha_text_detected():
    blocker = detect_blocker("https://x.com/apply", "Please verify you are human before continuing", [])
    assert blocker["request_type"] == "CAPTCHA_REQUIRED"


def test_login_text_detected_without_a_password_field():
    blocker = detect_blocker("https://x.com/apply", "Please sign in to continue your application", [])
    assert blocker["request_type"] == "LOGIN_REQUIRED"


def test_ordinary_page_has_no_blocker():
    elements = [_el(0, name="First name"), _el(1, name="Last name")]
    blocker = detect_blocker("https://x.com/apply", "Tell us about yourself", elements)
    assert blocker is None


def test_masked_destination_extracted_but_never_the_raw_address():
    blocker = detect_blocker(
        "https://x.com/verify",
        "We sent a verification code to j***@gmail.com — enter it below.",
        [_el(0, name="Verification code", autocomplete="one-time-code")],
    )
    assert blocker["masked_destination"] == "j***@gmail.com"


def test_submit_ref_prefers_a_verify_labeled_control():
    elements = [
        _el(0, name="Verification code", autocomplete="one-time-code"),
        _el(1, tag="button", type_="button", name="Verify"),
    ]
    blocker = detect_blocker("https://x.com/verify", "", elements)
    assert blocker["submit_ref"] == 1


def test_password_field_takes_priority_over_otp_text_in_surrounding_copy():
    """A page that both asks for a password AND happens to mention 'code'
    (e.g. a support blurb) should still be treated as a login wall, not OTP —
    password fields are the strongest, most unambiguous signal (Layer 1)."""
    elements = [_el(0, type_="password", name="Password")]
    blocker = detect_blocker("https://x.com/login", "Forgot your password? Use the recovery code in your email.", elements)
    assert blocker["request_type"] == "LOGIN_REQUIRED"
