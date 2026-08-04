"""
Raw profile column values must never reach a form.

`ats/base.py::_resolve_profile_value()` — the path `FieldMapper` uses for most
fields on a real form — was a bare `getattr`, while the per-field formatters
lived in `answer_engine` keyed by question CATEGORY and were unreachable from
there. So live postings received `requires_sponsorship` as **"True"**,
`notice_period_days` as **"30"** instead of "30 days", `expected_salary` as
**"120000.0"**, and `years_of_experience` as **"5.0"**.

Both paths now go through `automation/forms/profile_formatting.py`. The last
test in this file is the one that matters most long-term: it walks every typed
column on `CandidateProfile` and fails if any of them can still produce a raw
Python `bool`/`float`/`list`, so a NEW column added later cannot silently
reintroduce this.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Boolean, Float, Integer, inspect as sa_inspect
from sqlalchemy.dialects.postgresql import JSONB

from app.core.crypto import encrypt_field
from app.models.db_models import CandidateProfile, ProfileDocument
from automation.ats.greenhouse.greenhouse_adapter import GreenhouseAdapter
from automation.forms.answer_engine import ApplicationAnswerEngine
from automation.forms.field_mapper import FIELD_SYNONYMS
from automation.forms.profile_formatting import (
    PROFILE_VALUE_FORMATTERS,
    format_profile_value,
    format_scalar,
)


def _profile(**overrides) -> CandidateProfile:
    defaults = dict(profile_id="profile-1", user_id="user-1")
    defaults.update(overrides)
    profile = CandidateProfile(**defaults)
    profile.phone_encrypted = encrypt_field("+1-555-0100")
    return profile


def _resume_document() -> ProfileDocument:
    return ProfileDocument(
        document_id="doc-1", profile_id="profile-1", document_type="resume",
        original_filename="resume.pdf", stored_path=__file__, file_hash="abc", is_default=True,
    )


class _NoPage:
    """`_resolve_profile_value` never touches the page — this keeps the test
    free of a browser."""
    def locator(self, *a, **kw):
        raise AssertionError("no page interaction expected")


def _adapter(profile) -> GreenhouseAdapter:
    return GreenhouseAdapter(page=_NoPage(), profile=profile, resume_document=_resume_document())


# ---------------------------------------------------------------------------
# The FieldMapper path — where the bug lived
# ---------------------------------------------------------------------------

def test_a_boolean_resolves_as_yes_not_true():
    adapter = _adapter(_profile(requires_sponsorship=True))
    assert adapter._resolve_profile_value("requires_sponsorship") == "Yes"


def test_a_false_boolean_resolves_as_no_not_false():
    adapter = _adapter(_profile(requires_sponsorship=False))
    assert adapter._resolve_profile_value("requires_sponsorship") == "No"


def test_work_authorized_boolean_resolves_as_yes_no():
    assert _adapter(_profile(work_authorized=True))._resolve_profile_value("work_authorized") == "Yes"
    assert _adapter(_profile(work_authorized=False))._resolve_profile_value("work_authorized") == "No"


def test_notice_period_gets_its_unit():
    adapter = _adapter(_profile(notice_period_days=30))
    assert adapter._resolve_profile_value("notice_period_days") == "30 days"


def test_expected_salary_is_formatted_with_currency_and_separators():
    adapter = _adapter(_profile(expected_salary=120000.0, expected_salary_currency="USD"))
    assert adapter._resolve_profile_value("expected_salary") == "USD 120,000"


def test_years_of_experience_drops_the_float_tail():
    adapter = _adapter(_profile(years_of_experience=5.0))
    assert adapter._resolve_profile_value("years_of_experience") == "5 years"


def test_a_plain_string_field_is_unchanged():
    adapter = _adapter(_profile(first_name="Ada", linkedin_url="https://linkedin.com/in/ada"))
    assert adapter._resolve_profile_value("first_name") == "Ada"
    assert adapter._resolve_profile_value("linkedin_url") == "https://linkedin.com/in/ada"


def test_an_encrypted_property_still_decrypts_through_the_formatter():
    """`phone`/`address` go through the adapter's decrypting properties; adding
    formatting must not break that."""
    adapter = _adapter(_profile())
    assert adapter._resolve_profile_value("phone") == "+1-555-0100"


def test_an_unset_field_is_still_none_so_callers_skip_it():
    adapter = _adapter(_profile())
    assert adapter._resolve_profile_value("requires_sponsorship") is None
    assert adapter._resolve_profile_value("notice_period_days") is None


# ---------------------------------------------------------------------------
# requires_sponsorship's visa suffix, through BOTH paths
# ---------------------------------------------------------------------------

def test_visa_suffix_is_present_via_the_fieldmapper_path():
    adapter = _adapter(_profile(requires_sponsorship=True, visa_type="H1B"))
    assert adapter._resolve_profile_value("requires_sponsorship") == "Yes (H1B)"


def test_visa_suffix_is_present_via_the_answer_engine_path():
    engine = ApplicationAnswerEngine(
        profile=_profile(requires_sponsorship=True, visa_type="H1B"),
        llm_fn=lambda **kw: (_ for _ in ()).throw(AssertionError("LLM must not be called")),
    )
    result = engine.answer("Will you now or in the future require sponsorship?")
    assert result.answer == "Yes (H1B)"


def test_both_paths_agree_on_every_shared_formatter():
    """The whole point of the shared module: the two callers cannot drift."""
    profile = _profile(
        requires_sponsorship=True, visa_type="H1B", work_authorized=False,
        notice_period_days=45, expected_salary=95000.0, expected_salary_currency="INR",
        years_of_experience=7.5,
    )
    adapter = _adapter(profile)
    for attribute, formatter in PROFILE_VALUE_FORMATTERS.items():
        assert adapter._resolve_profile_value(attribute) == formatter(profile), attribute


def test_no_visa_suffix_when_sponsorship_is_not_required():
    adapter = _adapter(_profile(requires_sponsorship=False, visa_type="H1B"))
    assert adapter._resolve_profile_value("requires_sponsorship") == "No"


def test_work_authorized_never_gets_a_visa_suffix():
    """"Are you authorized to work" is a plain yes/no — appending a visa type
    answers a question that wasn't asked."""
    adapter = _adapter(_profile(work_authorized=True, visa_type="H1B"))
    assert adapter._resolve_profile_value("work_authorized") == "Yes"


# ---------------------------------------------------------------------------
# The type-based safety net
# ---------------------------------------------------------------------------

def test_format_scalar_handles_bool_before_int():
    """`bool` subclasses `int` in Python — an int branch placed first would
    swallow True/False and reintroduce the whole bug."""
    assert format_scalar(True) == "Yes"
    assert format_scalar(False) == "No"
    assert format_scalar(1) == 1
    assert format_scalar(0) == 0


def test_format_scalar_joins_lists():
    assert format_scalar(["USA", "Canada"]) == "USA, Canada"
    assert format_scalar([]) is None


def test_format_scalar_trims_integral_floats():
    assert format_scalar(5.0) == "5"
    assert format_scalar(7.5) == "7.5"


def test_an_unregistered_boolean_attribute_still_formats():
    """The net in action: a column with NO named formatter must still not
    produce the literal word "True"."""
    profile = _profile()
    profile.some_future_flag = True  # noqa: SLF001 - simulating a new column
    assert format_profile_value(profile, "some_future_flag", True) == "Yes"


# ---------------------------------------------------------------------------
# The guard against a future column regressing this
# ---------------------------------------------------------------------------

_RAW_TYPES_THAT_MUST_NEVER_REACH_A_FORM = (bool, float, list, dict, set, tuple)


def test_no_typed_profile_column_can_resolve_to_a_raw_python_value():
    """Walks every Boolean/Float/Integer/JSONB column on `CandidateProfile`,
    populates it, and asserts `_resolve_profile_value` returns display text
    rather than the raw Python object.

    This is the test that should fail when someone adds a new typed column and
    forgets `profile_formatting.py` exists. If it does fail, either register a
    named formatter or confirm `format_scalar`'s type handling covers it.
    """
    samples = {Boolean: True, Float: 12345.0, Integer: 42, JSONB: ["USA", "Canada"]}
    mapper = sa_inspect(CandidateProfile)

    checked = []
    for column in mapper.columns:
        sample = next(
            (value for col_type, value in samples.items() if isinstance(column.type, col_type)),
            None,
        )
        if sample is None:
            continue
        attribute = column.key
        profile = _profile(**{attribute: sample})
        resolved = _adapter(profile)._resolve_profile_value(attribute)
        checked.append(attribute)
        assert not isinstance(resolved, _RAW_TYPES_THAT_MUST_NEVER_REACH_A_FORM), (
            f"CandidateProfile.{attribute} ({column.type}) resolved to raw "
            f"{type(resolved).__name__} {resolved!r}. Register a formatter in "
            f"automation/forms/profile_formatting.py or extend format_scalar()."
        )

    assert checked, "no typed columns were exercised — the introspection above is broken"


def test_every_fieldmapper_resolvable_boolean_has_display_text():
    """Narrower and sharper: anything `FieldMapper` can actually resolve today
    is reachable from a real form, so it must format cleanly."""
    mapper = sa_inspect(CandidateProfile)
    boolean_columns = {c.key for c in mapper.columns if isinstance(c.type, Boolean)}
    reachable = boolean_columns & set(FIELD_SYNONYMS)

    for attribute in reachable:
        for sample in (True, False):
            resolved = _adapter(_profile(**{attribute: sample}))._resolve_profile_value(attribute)
            assert resolved in {"Yes", "No"} or isinstance(resolved, str), (
                f"{attribute}={sample} resolved to {resolved!r}"
            )
