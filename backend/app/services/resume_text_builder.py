from app.models.parsed_resume import ParsedResume


def build_resume_summary_text(parsed: ParsedResume) -> str:
    """
    Builds a clean, dense text representation of the resume for embedding.
    Prioritizes signal (skills, titles, achievements) over noise
    (contact info, formatting artifacts).
    """
    parts = []

    if parsed.total_years_experience is not None:
        parts.append(f"{parsed.total_years_experience} years of experience.")

    if parsed.skills:
        parts.append("Skills: " + ", ".join(parsed.skills) + ".")

    if parsed.inferred_skills:
        parts.append("Also familiar with: " + ", ".join(parsed.inferred_skills) + ".")

    for exp in parsed.experience:
        role_line = []
        if exp.title:
            role_line.append(exp.title)
        if exp.company:
            role_line.append(f"at {exp.company}")
        if role_line:
            parts.append(" ".join(role_line) + ".")
        if exp.description:
            parts.append(exp.description)
        if exp.achievements:
            parts.append("Achievements: " + " ".join(exp.achievements))

    for edu in parsed.education:
        edu_line = []
        if edu.degree:
            edu_line.append(edu.degree)
        if edu.field:
            edu_line.append(f"in {edu.field}")
        if edu.institution:
            edu_line.append(f"from {edu.institution}")
        if edu_line:
            parts.append(" ".join(edu_line) + ".")

    if parsed.certifications:
        parts.append("Certifications: " + ", ".join(parsed.certifications) + ".")

    return " ".join(parts)