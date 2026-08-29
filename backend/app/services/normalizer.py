from datetime import datetime
from app.models.parsed_resume import ParsedResume
from app.core.skills_taxonomy import canonicalize_skill


def normalize_skills(skills: list[str]) -> list[str]:
    """Canonicalize and deduplicate, preserving first-seen order."""
    seen = set()
    result = []
    for skill in skills:
        canonical = canonicalize_skill(skill)
        if canonical.lower() not in seen:
            seen.add(canonical.lower())
            result.append(canonical)
    return result


def _parse_year_month(date_str: str | None) -> tuple[int, int] | None:
    """Parses 'YYYY-MM' into (year, month). Returns None if unparseable or 'present'."""
    if not date_str or date_str.lower() == "present":
        return None
    try:
        dt = datetime.strptime(date_str, "%Y-%m")
        return dt.year, dt.month
    except ValueError:
        return None


def compute_total_years_experience(experience: list) -> float:
    """
    Computes total experience by summing date ranges ourselves,
    rather than trusting the LLM's arithmetic.
    Overlapping roles are NOT deduplicated in this version.
    """
    total_months = 0

    for exp in experience:
        start = _parse_year_month(exp.start_date)
        if not start:
            continue  # can't compute this role's duration — skip rather than guess

        if exp.end_date and exp.end_date.lower() == "present":
            end_year, end_month = datetime.now().year, datetime.now().month
        else:
            end = _parse_year_month(exp.end_date)
            if not end:
                continue
            end_year, end_month = end

        start_year, start_month = start
        months = (end_year - start_year) * 12 + (end_month - start_month)
        if months > 0:
            total_months += months

    return round(total_months / 12, 1)


def normalize_resume(parsed: ParsedResume) -> ParsedResume:
    """
    Applies normalization in place and returns the cleaned resume.
    """
    parsed.skills = normalize_skills(parsed.skills)
    parsed.inferred_skills = normalize_skills(parsed.inferred_skills)

    # Remove inferred skills that are already explicit — explicit skills win
    explicit_lower = {s.lower() for s in parsed.skills}
    parsed.inferred_skills = [s for s in parsed.inferred_skills if s.lower() not in explicit_lower]

    computed_years = compute_total_years_experience(parsed.experience)
    if computed_years > 0:
        parsed.total_years_experience = computed_years
    # else: leave whatever the LLM gave us (better than nothing if dates were unparseable)

    return parsed