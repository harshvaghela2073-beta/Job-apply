"""Skip rules for Naukri auto-apply, chosen by the candidate on 2026-10-13.

A job is not applied to (status ``skipped``, with the reason) when any rule matches:

1. Salary: Naukri shows a salary whose top is below the candidate's current CTC (profile field
   ``current_ctc``). A hidden salary ("Not disclosed") is fine.
2. Skills: the job's key skills include none of the candidate's skills (profile field ``skills``).
3. Fresher / internship-style: experience starts at 0 years and the text mentions internships,
   trainees, training or freshers.
4. Consultancy / individual recruiter: the company is hidden ("MNC's", "Reputed company", ...),
   its name says recruitment / placement / staffing / consultancy, or the poster is a person.

The facts come from the logged-in job page (``parse_job_facts``), falling back to what the search
stored on the job. Page selectors live in ``selectors.py`` (``JOB_*``).
"""

import re
from dataclasses import asdict, dataclass, field

from sqlmodel import Session, select

from app.models import ProfileField
from app.scanner.matching import parse_min_experience
from app.sources.html import first, select_any, soup, text_of, visible_text
from app.sources.naukri import selectors as S


@dataclass
class JobFacts:
    company: str = ""
    posted_by: str = ""
    salary: str = ""
    experience: str = ""
    skills: list[str] = field(default_factory=list)
    description: str = ""

    def as_dict(self) -> dict:
        data = asdict(self)
        data["description"] = data["description"][:300]
        return data


@dataclass(frozen=True)
class ApplyRules:
    min_salary_lpa: float | None  # skip when the shown salary's top is below this
    skills: tuple[str, ...]


def _clean(text: str) -> str:
    return " ".join(text.split())


def parse_job_facts(html: str) -> JobFacts:
    """Header facts of a logged-in Naukri job page (empty strings where not found)."""
    doc = soup(html)
    header = first(doc, S.JOB_HEADER) or doc
    facts = JobFacts()
    facts.company = _clean(text_of(first(header, S.JOB_COMPANY)))
    facts.salary = _clean(text_of(first(header, S.JOB_SALARY)))
    facts.experience = _clean(text_of(first(header, S.JOB_EXPERIENCE)))
    header_text = _clean(visible_text(str(header)))
    facts.posted_by = _posted_by(header)
    if not facts.salary:
        match = re.search(r"(Not Disclosed|[\d.,]+\s*(?:-|to)\s*[\d.,]+\s*(?:Lacs?|Lakhs?|LPA)\s*(?:P\.?\s*A\.?)?)", header_text, re.I)
        facts.salary = match.group(1) if match else ""
    facts.skills = [s for s in (_clean(text_of(node)) for node in select_any(doc, S.JOB_KEY_SKILLS)) if s][:40]
    facts.description = _clean(text_of(first(doc, S.JOB_DESCRIPTION)))[:3000]
    return facts


def _posted_by(header) -> str:
    """The name after "Posted by", read from the smallest element that holds both."""
    for string in header.find_all(string=re.compile(r"posted by", re.I)):
        holder = string.parent
        while holder is not None:
            text = _clean(holder.get_text(" "))
            if len(text) > 120:
                break
            match = re.search(r"posted by\s*:?\s*(.+)", text, re.I)
            if match and match.group(1).strip():
                return match.group(1).strip()
            holder = holder.parent
    return ""


# --- salary -----------------------------------------------------------------------------------

_LAKH_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*(?:lacs?|lakhs?|lpa|l\b)", re.I)
_LAKH_SINGLE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:lacs?|lakhs?|lpa)", re.I)
_RUPEE_RANGE = re.compile(r"(\d{1,3}(?:,\d{2,3})+)\s*(?:-|to)\s*(\d{1,3}(?:,\d{2,3})+)")


def salary_top_lpa(text: str | None) -> float | None:
    """Top of a salary label in lakhs a year: "2.5-4.5 Lacs P.A." -> 4.5; None when hidden/unknown."""
    if not text or "not disclosed" in text.lower():
        return None
    if match := _LAKH_RANGE.search(text):
        return float(match.group(2))
    if match := _RUPEE_RANGE.search(text):
        return int(match.group(2).replace(",", "")) / 100_000
    if match := _LAKH_SINGLE.search(text):
        return float(match.group(1))
    return None


def parse_lpa(text: str | None) -> float | None:
    """"12 LPA" / "12" / "12 Lakhs" -> 12.0."""
    match = re.search(r"\d+(?:\.\d+)?", text or "")
    return float(match.group()) if match else None


# --- skills -----------------------------------------------------------------------------------

_ALIASES = {
    "golang": ("golang", "go"),
    "go": ("golang", "go"),
    "spring boot": ("spring boot", "springboot", "spring"),
    "apache kafka": ("kafka",),
    "kafka": ("kafka",),
    "kubernetes": ("kubernetes", "k8s"),
    "gke": ("gke", "google kubernetes engine"),
    "sql": ("sql", "mysql", "postgresql", "postgres", "pl/sql", "sql server"),
}


def _variants(skill: str) -> tuple[str, ...]:
    lowered = skill.strip().lower()
    return _ALIASES.get(lowered, (lowered,))


def _has(haystack: str, needle: str) -> bool:
    return re.search(rf"(?<![a-z0-9+#]){re.escape(needle)}(?![a-z0-9+#])", haystack) is not None


def matching_skills(mine: tuple[str, ...], job_skills: list[str]) -> list[str]:
    haystack = " , ".join(job_skills).lower()
    return [skill for skill in mine if any(_has(haystack, v) for v in _variants(skill))]


# --- freshers / consultancies --------------------------------------------------------------------

_FRESHER = re.compile(r"\b(internships?|interns?|trainees?|training|freshers?)\b", re.I)
_CONSULTANCY = re.compile(
    r"\b(recruit\w*|placements?|staffing|manpower|consultancy(?!\s+services)|consultants|hr services|hr solutions|headhunt\w*)\b", re.I
)
_PLACEHOLDER_COMPANY = re.compile(
    r"^(an? )?(top |leading |reputed |well[- ]known |large |big )*(mnc'?s?|company|firm|organi[sz]ation|client|"
    r"it company|product company|startup|confidential)\b",
    re.I,
)
_COMPANY_WORDS = {
    "technologies", "technology", "tech", "solutions", "labs", "systems", "software", "services", "consulting",
    "group", "media", "digital", "infotech", "infosystems", "networks", "global", "india", "limited", "ltd", "pvt",
    "private", "inc", "llp", "corp", "corporation", "analytics", "ventures", "studio", "studios", "works", "health",
    "finance", "capital", "bank", "ai", "data", "cloud", "logistics", "retail", "pharma", "energy", "motors",
    "industries", "enterprises", "partners", "company", "innovations", "innovation", "foods", "payments", "games",
}


def looks_like_person(name: str) -> bool:
    words = name.strip().split()
    if not 2 <= len(words) <= 3 or not all(re.fullmatch(r"[A-Z][a-z]+", w) for w in words):
        return False
    return not any(w.lower() in _COMPANY_WORDS for w in words)


def skip_reason(rules: ApplyRules, facts: JobFacts, *, stored_company: str = "", stored_text: str = "",
                stored_experience: str = "") -> str | None:
    """Why auto-apply should skip this job, or None to apply."""
    # 1. Salary below the current CTC.
    top = salary_top_lpa(facts.salary)
    if rules.min_salary_lpa is not None and top is not None and top < rules.min_salary_lpa:
        return f"Salary {facts.salary} is below your current {rules.min_salary_lpa:g} LPA."

    # 2. None of the candidate's skills.
    job_skills = facts.skills or [s for s in stored_text.split(" · ")[0].split(",") if s.strip()]
    if rules.skills and job_skills and not matching_skills(rules.skills, job_skills):
        return "None of your skills are in the job's key skills (" + ", ".join(job_skills[:6]) + ")."

    # 3. Fresher / internship-style.
    min_exp = parse_min_experience(facts.experience or stored_experience or "")
    text = f"{facts.description} {stored_text}"
    if min_exp == 0 and (match := _FRESHER.search(text)):
        return f"Fresher / internship-style posting (0 years, mentions \"{match.group(0).lower()}\")."

    # 4. Consultancy or individual recruiter.
    company = facts.company or stored_company
    poster = facts.posted_by
    for name in (company, poster, stored_company):
        if name and _CONSULTANCY.search(name):
            return f"Posted by a consultancy ({name})."
    if company and _PLACEHOLDER_COMPANY.match(company):
        return f"The company is hidden (\"{company}\"); posted by {poster or stored_company or 'a consultancy'}."
    for name in (poster, company, stored_company):
        if name and looks_like_person(name):
            return f"Posted by an individual recruiter ({name})."
    return None


def load_rules(session: Session) -> ApplyRules:
    fields = {f.key: f.value for f in session.exec(select(ProfileField)).all()}
    skills = tuple(s.strip() for s in (fields.get("skills") or "").split(",") if s.strip())
    return ApplyRules(min_salary_lpa=parse_lpa(fields.get("current_ctc")), skills=skills)
