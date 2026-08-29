from pydantic import BaseModel, Field
from typing import Optional


class Experience(BaseModel):
    company: Optional[str] = None
    title: Optional[str] = None
    start_date: Optional[str] = None  # "YYYY-MM"
    end_date: Optional[str] = None    # "YYYY-MM" or "present"
    description: Optional[str] = None
    achievements: list[str] = Field(default_factory=list)


class Education(BaseModel):
    institution: Optional[str] = None
    degree: Optional[str] = None
    field: Optional[str] = None
    graduation_year: Optional[int] = None


class ParsedResume(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    total_years_experience: Optional[float] = None
    skills: list[str] = Field(default_factory=list)
    inferred_skills: list[str] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)