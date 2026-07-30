from app.core.skills_taxonomy import canonicalize_skill


def compute_skill_gap(candidate_skills: list[str], candidate_inferred: list[str], required_skills: list[str]) -> dict:
    """
    Compares job-required skills against the candidate's normalized skills.
    Explicit + inferred candidate skills both count as "known" —
    inferred skills are still real capability signal, just lower-confidence.
    """
    candidate_set = {canonicalize_skill(s).lower() for s in candidate_skills + candidate_inferred}
    required_canonical = [canonicalize_skill(s) for s in required_skills]

    matched = [s for s in required_canonical if s.lower() in candidate_set]
    missing = [s for s in required_canonical if s.lower() not in candidate_set]

    overlap_ratio = len(matched) / len(required_canonical) if required_canonical else 1.0

    return {
        "matched_skills": matched,
        "missing_skills": missing,
        "overlap_ratio": round(overlap_ratio, 2),
    }