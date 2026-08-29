"""
`AutonomousTask.candidate_profile` is a JSONB column, so everything written to
it must be JSON-encodable.

Regression origin (observed live, not hypothetical): `POST /agent/tasks`
returned a hard 500 for any user who actually had a `CandidateProfile` row —

    sqlalchemy.exc.StatementError: (builtins.TypeError)
    Object of type datetime is not JSON serializable
    [SQL: INSERT INTO autonomous_tasks (... candidate_profile ...)]

`profile_repository.profile_to_dict` is documented as returning a dict "ready
for a Pydantic response model", where `created_at`/`updated_at` as real
`datetime` objects are correct — Pydantic serializes them. Four of its five
callers build a `ProfileResponse` and want exactly that. The fifth,
`_build_candidate_profile_snapshot`, was putting the same dict straight into
JSONB, where psycopg2's encoder rejects a `datetime`.

Why no existing test caught it: the API-level tests monkeypatch
`_build_candidate_profile_snapshot` wholesale, and the browser E2E fixtures
create a user WITHOUT a `CandidateProfile` row — with no profile,
`profile_to_dict` is never called and the snapshot's `profile` is `None`. So
the failure needed a real profile to appear, which is exactly what production
had and the tests did not.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

from app.api.autonomous_agent import _PROFILE_SNAPSHOT_EXCLUDED_FIELDS, _json_safe_profile


def _profile_like_dict():
    """The shape `profile_to_dict` really returns — verified against a live row:
    every value JSON-safe except `created_at`/`updated_at`."""
    return {
        "profile_id": "prof_1",
        "user_id": "user_1",
        "full_name": "Ada Lovelace",
        "email": "ada@example.com",
        "years_of_experience": 1.0,
        "skills": None,
        "sponsorship_countries": ["UK"],
        "autopilot_globally_disabled": False,
        "created_at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 6, 7, 8, 9, 10, tzinfo=timezone.utc),
    }


def test_the_raw_profile_dict_is_not_json_encodable():
    """Pins the premise. If `profile_to_dict` ever became JSON-safe on its own,
    this test failing is the signal that the sanitizer is now redundant rather
    than load-bearing."""
    try:
        json.dumps(_profile_like_dict())
    except TypeError as exc:
        assert "datetime" in str(exc)
    else:
        raise AssertionError("profile_to_dict output is now JSON-safe — revisit _json_safe_profile")


def test_the_sanitized_snapshot_is_json_encodable():
    """The actual regression: this is what goes into the JSONB column."""
    json.dumps(_json_safe_profile(_profile_like_dict()))


def test_row_bookkeeping_is_dropped_not_stringified():
    """`created_at`/`updated_at` say when OUR database row was written. No job
    application form asks that, and leaving them in as ISO strings would put two
    dates in the decision prompt the model could only misuse — e.g. as an
    availability date."""
    safe = _json_safe_profile(_profile_like_dict())
    assert "created_at" not in safe
    assert "updated_at" not in safe
    assert _PROFILE_SNAPSHOT_EXCLUDED_FIELDS == {"created_at", "updated_at"}


def test_every_other_field_survives_unchanged():
    """The fix must not cost the agent any information it needs to fill a form."""
    raw = _profile_like_dict()
    safe = _json_safe_profile(raw)
    expected = {k: v for k, v in raw.items() if k not in _PROFILE_SNAPSHOT_EXCLUDED_FIELDS}
    assert safe == expected


def test_a_future_datetime_or_date_column_is_iso_formatted_not_dropped():
    """The excluded set is about relevance; the type coercion is the safety net.
    A NEW date-ish profile column (an availability date, say) is real answer
    material, so it must reach the agent — as a string, not as a 500."""
    raw = {"earliest_start": date(2026, 9, 1), "verified_at": datetime(2026, 9, 1, 12, 0)}
    safe = _json_safe_profile(raw)
    assert safe == {"earliest_start": "2026-09-01", "verified_at": "2026-09-01T12:00:00"}
    json.dumps(safe)


def test_an_unexpected_unencodable_type_degrades_instead_of_500ing(caplog):
    """A `Decimal` salary column, or anything else unencodable, must not turn
    task creation into a 500 — but it must be noisy, because the agent would
    otherwise be reasoning about a value nobody designed for it."""
    safe = _json_safe_profile({"expected_salary": Decimal("120000.50")})
    assert safe == {"expected_salary": "120000.50"}
    json.dumps(safe)
    assert any("not JSON" in r.message or "not JSON" in r.getMessage() for r in caplog.records)


def test_a_missing_profile_stays_none():
    """A user with no profile row is normal, and must not become `{}` — the
    snapshot's consumers distinguish "no profile" from "an empty profile"."""
    assert _json_safe_profile(None) is None
