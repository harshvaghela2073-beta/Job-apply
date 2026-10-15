"""Skip rules for LinkedIn Easy Apply: the candidate's four Naukri rules (2026-10-13), adapted to
LinkedIn's job page. A job is not applied to (status ``skipped``, with the reason) when:

1. Salary: the page shows a salary ("₹12,00,000/yr - ₹18,00,000/yr", "₹15L/yr") whose top is below
   the candidate's current CTC (profile field ``current_ctc``). No salary shown is fine.
2. Skills: none of the candidate's skills (profile field ``skills``) appears in the job's title or
   description (whole words; aliases such as Go/Golang count).
3. Fresher / internship: LinkedIn's level says "Internship", or the minimum experience is 0 years
   and the text mentions internships, trainees, training or freshers.
4. Consultancy: the company name says recruitment / placement / staffing / consultancy, the company
   is hidden ("Confidential", "Reputed MNC"), or its industry is "Staffing and Recruiting".

The salary, skill and wording helpers are shared with Naukri (``app/sources/naukri/screening.py``);
page selectors live in ``selectors.py`` (``JOB_*``).
"""

import re
from dataclasses import asdict, dataclass

from app.scanner.applying import StoredJob
from app.scanner.matching import is_internship, parse_min_experience
from app.sources.html import first, select_any, soup, text_of
from app.sources.linkedin import selectors as S
from app.sources.naukri.screening import (  # pure text rules, shared with Naukri
    _CONSULTANCY,
    _FRESHER,
    _PLACEHOLDER_COMPANY,
    ApplyRules,
    load_rules,  # noqa: F401 - re-exported for the applier
    matching_skills,
    salary_top_lpa,
)


@dataclass
class LinkedInJobFacts:
    title: str = ""
    company: str = ""
    salary: str = ""
    level: str = ""  # "Entry level", "Internship", ...
    industry: str = ""  # "Staffing and Recruiting" when the company box says so
    description: str = ""
    posted: str = ""  # the top card's age, e.g. "2 weeks ago" (2026-10-15)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["description"] = data["description"][:300]
        return data


def _clean(text: str) -> str:
    return " ".join(text.split())


# ₹12,00,000/yr, ₹15L/yr, ₹1.2M/yr, ₹50K/mo, ₹500/hr; since 2026-10-14 also "1M INR/yr - 3M INR/yr".
_UNITS = r"(cr|crore|lakhs?|lacs?|l|m|k)?"
_AMOUNT = re.compile(
    rf"(?:₹\s*(\d[\d,]*(?:\.\d+)?)\s*{_UNITS}|(\d[\d,]*(?:\.\d+)?)\s*{_UNITS}\s*INR)\s*/\s*(yr|year|mo|month|hr|hour)\b",
    re.I,
)
_UNIT = {"cr": 1e7, "crore": 1e7, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "l": 1e5, "m": 1e6, "k": 1e3}
_PER_YEAR = {"yr": 1, "year": 1, "mo": 12, "month": 12, "hr": 2080, "hour": 2080}
_ONE_SALARY = r"(?:₹[^₹]*?|\d[\d,.]*\s*(?:cr|crore|lakhs?|lacs?|l|m|k)?\s*INR\s*)/\s*(?:yr|year|mo|month|hr|hour)\b"
# "2 weeks ago", "Reposted 3 days ago", "5 hours ago" in the top card.
_POSTED_TEXT = re.compile(
    r"\b(?:reposted\s+)?(?:\d+|an?)\s+(?:minute|hour|day|week|month|year)s?\s+ago\b", re.I
)
_SALARY_TEXT = re.compile(rf"{_ONE_SALARY}(?:\s*[-–]\s*{_ONE_SALARY})?", re.I)


def linkedin_salary_top_lpa(text: str | None) -> float | None:
    """Top of a LinkedIn salary in lakhs a year: "₹12,00,000/yr - ₹18,00,000/yr" -> 18,
    "₹15L/yr" -> 15, "₹50K/mo" -> 6. Falls back to Naukri-style labels ("12-18 LPA")."""
    if not text:
        return None
    amounts = []
    for rupee_number, rupee_unit, inr_number, inr_unit, period in _AMOUNT.findall(text):
        number, unit = (rupee_number, rupee_unit) if rupee_number else (inr_number, inr_unit)
        value = float(number.replace(",", "")) * _UNIT.get(unit.lower(), 1) * _PER_YEAR[period.lower()]
        amounts.append(value / 1e5)
    if amounts:
        return round(max(amounts), 2)
    return salary_top_lpa(text)


def _heading_block(doc, heading: str):
    """The block around a heading found by its text (the 2026-10-14 layout has no class names)."""
    for string in doc.find_all(string=lambda s: s is not None and s.strip().lower() == heading):
        box = string.parent
        for _ in range(5):
            if box is None or box.parent is None:
                break
            box = box.parent
            if len(_clean(box.get_text(" ", strip=True))) > len(heading) + 40:
                return box
    return None


def _new_layout_facts(doc, facts: LinkedInJobFacts) -> None:
    """Fill what the older selectors didn't find, the 2026-10-14 way (``selectors.JOB_DETAILS_SCREEN``)."""
    title_tag = _clean(doc.title.get_text()) if doc.title is not None else ""
    parts = [part.strip() for part in title_tag.split(" | ")]
    if len(parts) >= 3 and parts[-1].lower() == S.PAGE_TITLE_SUFFIX:
        facts.title = facts.title or parts[0]
        facts.company = facts.company or " | ".join(parts[1:-1])
    screen = first(doc, S.JOB_DETAILS_SCREEN)
    if screen is None:
        return
    if not facts.description:
        about = _heading_block(screen, S.ABOUT_JOB_HEADING)
        if about is not None:
            box = first(about, S.SDUI_POST_TEXT)  # the expandable text box holds the description
            text = _clean((box if box is not None else about).get_text(" ", strip=True))
            facts.description = re.sub(r"^about the job\s*", "", text, flags=re.I)[:5000]
    text = _clean(screen.get_text(" ", strip=True))
    lowered = text.lower()
    end = min((i for i in (lowered.find(h) for h in S.BELOW_TOP_CARD) if i > 0), default=min(len(text), 3000))
    if not facts.salary:
        match = _SALARY_TEXT.search(text[:end])
        if match:
            facts.salary = _clean(match.group())
    if not facts.posted:
        match = _POSTED_TEXT.search(text[:end])
        if match:
            facts.posted = _clean(match.group())
    if not facts.industry:
        company_box = _heading_block(screen, S.ABOUT_COMPANY_HEADING)
        if company_box is not None and S.STAFFING_INDUSTRY in _clean(company_box.get_text(" "))[:400].lower():
            facts.industry = "Staffing and Recruiting"


def parse_job_facts(html: str) -> LinkedInJobFacts:
    """Facts from a logged-in LinkedIn job page (empty strings where not found)."""
    doc = soup(html)
    facts = LinkedInJobFacts()
    facts.title = _clean(text_of(first(doc, S.JOB_TITLE)))
    facts.company = _clean(text_of(first(doc, S.JOB_COMPANY)))
    insights = " | ".join(_clean(text_of(node)) for node in select_any(doc, S.JOB_INSIGHTS))
    top = _clean(text_of(first(doc, S.JOB_TOP_CARD)))
    for text in (insights, top):
        match = _SALARY_TEXT.search(text)
        if match:
            facts.salary = _clean(match.group())
            break
    lowered = insights.lower()  # the pills only: the title ("Associate Engineer") is in the top card
    facts.level = next((level.capitalize() for level in S.JOB_LEVELS if level in lowered), "")
    company_box = text_of(first(doc, S.JOB_COMPANY_BOX)).lower()
    if S.STAFFING_INDUSTRY in company_box:
        facts.industry = "Staffing and Recruiting"
    description = first(doc, S.JOB_DESCRIPTION)
    facts.description = _clean(description.get_text(" ", strip=True))[:5000] if description is not None else ""
    _new_layout_facts(doc, facts)
    return facts


def skip_reason(rules: ApplyRules, facts: LinkedInJobFacts, stored: StoredJob | None = None) -> str | None:
    """Why auto-apply should skip this job, or None to apply."""
    stored = stored or StoredJob(url="")
    # 1. Salary below the current CTC.
    top = linkedin_salary_top_lpa(facts.salary)
    if rules.min_salary_lpa is not None and top is not None and top < rules.min_salary_lpa:
        return f"Salary {facts.salary} is below your current {rules.min_salary_lpa:g} LPA."

    # 2. None of the candidate's skills in the title and description.
    title = facts.title or stored.title
    if rules.skills and facts.description and not matching_skills(rules.skills, [title, facts.description]):
        return "None of your skills are in the job's title or description."

    # 3. Fresher / internship.
    if facts.level.lower() == "internship" or is_internship(title):
        return "Internship posting."
    min_exp = parse_min_experience(f"{title} {facts.description}")
    if min_exp == 0 and (match := _FRESHER.search(f"{title} {facts.description}")):
        return f"Fresher / internship-style posting (0 years, mentions \"{match.group(0).lower()}\")."

    # 4. Consultancy.
    company = facts.company or stored.company
    if company and _CONSULTANCY.search(company):
        return f"Posted by a consultancy ({company})."
    if company and _PLACEHOLDER_COMPANY.match(company):
        return f"The company is hidden (\"{company}\")."
    if facts.industry:
        return f"Posted by a consultancy ({company or 'the company'} is in {facts.industry})."
    return None
