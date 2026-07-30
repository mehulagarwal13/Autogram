import re


def _normalize_for_matching(text: str) -> str:
    return re.sub(r"[^a-z0-9\s]", " ", text.lower())


def compute_keyword_match_score(resume_raw_text: str, ats_keywords: list[str]) -> dict:
    """
    Checks literal (case-insensitive) presence of each ATS keyword in the
    resume's RAW extracted text — not the normalized skill list. This
    deliberately mirrors how real ATS keyword scanners behave: they look
    for the exact term as written in the job post, not a canonicalized
    synonym. A resume that says "JS" will not match a job asking for
    "JavaScript" here, even though our semantic matcher (Node 9) treats
    them as equivalent — that gap is realistic and worth surfacing to the user.
    And that's how this node will execute 
    """
    normalized_resume = _normalize_for_matching(resume_raw_text)

    found = []
    missing = []
    for keyword in ats_keywords:
        normalized_keyword = _normalize_for_matching(keyword)
        if normalized_keyword and normalized_keyword in normalized_resume:
            found.append(keyword)
        else:
            missing.append(keyword)

    ratio = len(found) / len(ats_keywords) if ats_keywords else 1.0

    return {
        "found_keywords": found,
        "missing_keywords": missing,
        "keyword_match_ratio": round(ratio, 2),
    }


STANDARD_SECTION_HEADERS = [
    "experience", "work experience", "employment", "education",
    "skills", "summary", "objective", "certifications", "projects",
]


def compute_format_score(resume_raw_text: str) -> dict:
    """
    Heuristic parseability score — approximates what real ATS parsers check:
    can standard sections be found, is there enough extractable text, etc.
    This is NOT a design/graphics analysis (we don't have the visual layout,
    only extracted text) — it's a proxy based on textual structure.
    """
    normalized = resume_raw_text.lower()

    sections_found = [h for h in STANDARD_SECTION_HEADERS if h in normalized]
    section_score = min(len(sections_found) / 3, 1.0)  # 3+ standard sections = full score

    length_score = 1.0 if len(resume_raw_text.strip()) > 200 else 0.5

    format_score = round((section_score * 0.7) + (length_score * 0.3), 2)

    return {
        "sections_detected": sections_found,
        "format_score": format_score,
    }


def compute_ats_score(resume_raw_text: str, ats_keywords: list[str]) -> dict:
    """
    Combines keyword matching (70%) and format/parseability (30%) into
    a single ATS score. Keyword presence matters more since that's the
    dominant factor in most real ATS ranking algorithms.
    """
    keyword_result = compute_keyword_match_score(resume_raw_text, ats_keywords)
    format_result = compute_format_score(resume_raw_text)

    ats_score = round(
        (keyword_result["keyword_match_ratio"] * 0.7) + (format_result["format_score"] * 0.3),
        2,
    )

    return {
        "ats_score": ats_score,
        "keyword_match_ratio": keyword_result["keyword_match_ratio"],
        "found_keywords": keyword_result["found_keywords"],
        "missing_keywords": keyword_result["missing_keywords"],
        "format_score": format_result["format_score"],
        "sections_detected": format_result["sections_detected"],
    }