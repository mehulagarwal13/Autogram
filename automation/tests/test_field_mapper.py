"""
FieldMapper (automation/forms/field_mapper.py) — Phase 5. Pure-logic tests,
no browser needed: `map_field()` operates on plain strings, not DOM elements
(the DOM-facing side of Phase 5 — pulling label/name/placeholder text off a
real page — is exercised in test_greenhouse_adapter.py / test_lever_adapter.py
via `ATSAdapter._fill_known_questions`).
"""

from automation.forms.field_mapper import (
    LABEL_MATCH_CONFIDENCE,
    NAME_MATCH_CONFIDENCE,
    NEARBY_TEXT_MATCH_CONFIDENCE,
    PLACEHOLDER_MATCH_CONFIDENCE,
    FieldMapper,
)


# ---------- basic per-signal matching ----------

def test_matches_by_label_text():
    result = FieldMapper.map_field(label="First Name")
    assert result == ("first_name", LABEL_MATCH_CONFIDENCE)


def test_matches_by_placeholder():
    result = FieldMapper.map_field(placeholder="Email address")
    assert result == ("email", PLACEHOLDER_MATCH_CONFIDENCE)


def test_matches_by_nearby_text():
    result = FieldMapper.map_field(nearby_text="Please provide your current location")
    assert result == ("location", NEARBY_TEXT_MATCH_CONFIDENCE)


def test_matches_by_name_attribute():
    result = FieldMapper.map_field(name="first_name")
    assert result == ("first_name", NAME_MATCH_CONFIDENCE)


def test_returns_none_when_nothing_matches():
    assert FieldMapper.map_field(label="Why do you want to work here?") is None
    assert FieldMapper.map_field() is None


# ---------- name/id normalization ----------

def test_name_attribute_handles_camel_case():
    assert FieldMapper.map_field(name="firstName") == ("first_name", NAME_MATCH_CONFIDENCE)


def test_name_attribute_handles_snake_case():
    assert FieldMapper.map_field(name="last_name") == ("last_name", NAME_MATCH_CONFIDENCE)


def test_name_attribute_handles_kebab_case():
    assert FieldMapper.map_field(name="last-name") == ("last_name", NAME_MATCH_CONFIDENCE)


def test_name_attribute_handles_bracket_notation():
    assert FieldMapper.map_field(name="job_application[first_name]") == ("first_name", NAME_MATCH_CONFIDENCE)


def test_name_attribute_handles_dot_notation():
    assert FieldMapper.map_field(name="candidate.email") == ("email", NAME_MATCH_CONFIDENCE)


def test_name_attribute_bracket_notation_uses_the_last_segment():
    # "job_application[urls][LinkedIn]" — the meaningful part is the last
    # bracket segment, not "job_application" or "urls".
    assert FieldMapper.map_field(name="job_application[urls][LinkedIn]") == ("linkedin_url", NAME_MATCH_CONFIDENCE)


# ---------- label normalization ----------

def test_label_strips_a_trailing_required_field_asterisk():
    assert FieldMapper.map_field(label="LinkedIn Profile*") == ("linkedin_url", LABEL_MATCH_CONFIDENCE)


def test_label_strips_asterisk_even_with_trailing_whitespace():
    assert FieldMapper.map_field(label="  LinkedIn Profile*  ") == ("linkedin_url", LABEL_MATCH_CONFIDENCE)


def test_label_matching_is_case_insensitive():
    assert FieldMapper.map_field(label="FIRST NAME") == ("first_name", LABEL_MATCH_CONFIDENCE)


# ---------- tiering: name > label > placeholder > nearby_text ----------

def test_name_wins_over_label_when_both_are_supplied():
    result = FieldMapper.map_field(label="First Name", name="email")
    assert result == ("email", NAME_MATCH_CONFIDENCE)


def test_label_wins_over_placeholder_when_both_are_supplied():
    result = FieldMapper.map_field(label="Email Address", placeholder="Phone number")
    assert result == ("email", LABEL_MATCH_CONFIDENCE)


def test_placeholder_wins_over_nearby_text_when_both_are_supplied():
    result = FieldMapper.map_field(placeholder="Email address", nearby_text="phone number")
    assert result == ("email", PLACEHOLDER_MATCH_CONFIDENCE)


def test_falls_through_to_a_lower_tier_when_the_higher_tier_has_no_match():
    # name doesn't match anything, but label does — label tier still applies.
    result = FieldMapper.map_field(label="First Name", name="q_1234")
    assert result == ("first_name", LABEL_MATCH_CONFIDENCE)


# ---------- the documented ambiguity trade-off ----------

def test_a_short_ambiguous_name_attribute_does_not_guess():
    # "company" alone doesn't match current_company's multi-word synonyms —
    # deliberately: see the module docstring's trade-off note.
    assert FieldMapper.map_field(name="company") is None
    assert FieldMapper.map_field(name="title") is None


# ---------- newly-added profile attributes ----------

def test_matches_city_state_country_and_address():
    assert FieldMapper.map_field(label="City") == ("city", LABEL_MATCH_CONFIDENCE)
    assert FieldMapper.map_field(label="State") == ("state", LABEL_MATCH_CONFIDENCE)
    assert FieldMapper.map_field(label="Country") == ("country", LABEL_MATCH_CONFIDENCE)
    assert FieldMapper.map_field(label="Street Address") == ("address", LABEL_MATCH_CONFIDENCE)


def test_matches_website_url_distinctly_from_portfolio_url():
    assert FieldMapper.map_field(label="Personal Website") == ("portfolio_url", LABEL_MATCH_CONFIDENCE)
    assert FieldMapper.map_field(label="Website URL") == ("website_url", LABEL_MATCH_CONFIDENCE)
