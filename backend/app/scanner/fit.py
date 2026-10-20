"""Job-fit rules shared by every scanner and applier, approved by the candidate on 2026-10-15 to send
fewer, better-fitting applications (docs/API_CONTRACT.md, "Job fit").

- **Another stack:** the title names a technology or track from ``OFF_STACK`` and none of the
  candidate's skills (profile field ``skills``). "Java Full Stack Developer (Spring Boot + Angular)" is
  kept (Java), "Full Stack Developer - .Net / React.js" isn't. A database dialect that belongs to
  another stack ("SQL Server", "PL/SQL") doesn't count as the candidate's SQL here.
- **Senior:** the title says Senior / Sr / Lead / Principal / Staff / Architect / Manager / Head /
  Director / VP (whole words), or level 3 and up (SDE 3, SDE III, SDE-3, Software Engineer III,
  Level 3, L3). SDE 2 and Software Engineer II are fine. Phrases that aren't the role's level, such as
  "hiring manager", "head over to", "you will lead", "work with senior engineers" and "Member of
  Technical Staff", don't count.
- **Skip list:** the company name contains an entry of the profile field ``search_skip_companies``
  (comma-separated, case-insensitive; missing means empty).
- **Skills:** at least ``MIN_SKILLS`` of the candidate's skills in the job (``matching_skills``, with
  aliases such as Go/Golang and MySQL/SQL). In running text a bare "Go" counts only next to a list
  separator or a tech word ("Java, Go", "(Go)", "in Go", "Go developer"): "go live" and "go-to-market"
  don't.

Where they apply: at search time (``linkedin_jobs``, ``naukri_jobs``: title rules and skip list, the
job isn't stored and the run message counts it), at apply time (both platforms' ``skip_reason``: all
four) and for hiring posts (skip list on the post's company, senior rule on the line naming the role).
"""

import re
from collections.abc import Iterable

from app.scanner.matching import SearchPrefs, split_list

MIN_SKILLS = 2

# ---------------------------------------------------------------------------
# Skills

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
# "Go" written as the language in running text: next to a list separator or a tech word.
_GO_IN_TEXT = re.compile(
    r"(?:[,/(|&+]|\b(?i:and|or|in|with|using|like|java|python))\s*(?:Go|GO)(?![A-Za-z0-9+#-])"
    r"(?!\s+(?i:to|live|ahead|beyond|through|for|out|back|over|on)\b)"
    r"|(?<![A-Za-z0-9+#-])(?:Go|GO)(?!\s*/\s*(?i:no)\b)(?=\s*[,/)|&]|\s+(?i:developers?|engineers?|programming|"
    r"language|lang|services|microservices|backend|routines|code|modules|or)\b)"
)


def _variants(skill: str) -> tuple[str, ...]:
    lowered = skill.strip().lower()
    return _ALIASES.get(lowered, (lowered,))


def _has(haystack: str, needle: str) -> bool:
    return re.search(rf"(?<![a-z0-9+#]){re.escape(needle)}(?![a-z0-9+#])", haystack) is not None


def _in_text(text: str, lowered: str, variant: str) -> bool:
    if variant == "go":
        return _GO_IN_TEXT.search(text) is not None
    return _has(lowered, variant)


def matching_skills(mine: Iterable[str], job_skills: list[str], text: str = "") -> list[str]:
    """The candidate's skills (``mine``, in their order) found in the job's key-skill tags
    (``job_skills``, each tag as written) or in running text (``text``: title, description)."""
    tags = " , ".join(job_skills).lower()
    lowered = text.lower()
    return [
        skill for skill in mine
        if any(_has(tags, v) or (text and _in_text(text, lowered, v)) for v in _variants(skill))
    ]


def skills_needed(mine: tuple[str, ...]) -> int:
    """How many of the candidate's skills a job must name: ``MIN_SKILLS``, or fewer with a shorter list."""
    return min(MIN_SKILLS, len(mine))


def too_few_skills(found: list[str], where: str, mine: tuple[str, ...]) -> str | None:
    """The skip reason when the job names fewer than ``skills_needed`` of the candidate's skills."""
    if len(found) >= skills_needed(mine):
        return None
    if not found:
        return f"None of your skills are in {where}."
    return f"Only 1 of your skills ({found[0]}) is in {where}; at least {MIN_SKILLS} are needed."


# ---------------------------------------------------------------------------
# Another stack

# (label, pattern): technologies and tracks outside the candidate's stack (2026-10-15). Patterns are
# case-insensitive whole words; the third item, when given, is a pattern that cancels the term
# ("Frontend" only for frontend-only titles).
OFF_STACK: tuple[tuple[str, str, str | None], ...] = (
    ("Android", r"android", None),
    ("iOS", r"ios", None),
    ("Swift", r"swift(?:ui)?", None),
    ("Flutter", r"flutter", None),
    ("React Native", r"react[\s-]*native", None),
    ("Kotlin", r"kotlin", None),
    (".NET", r"\.net(?:\s*core)?|asp\.net|vb\.net|dot[\s-]?net|c#", None),
    ("PHP", r"php|laravel", None),
    ("Ruby", r"ruby|rails|ror", None),
    ("Salesforce", r"salesforce|sfdc", None),
    ("SAP", r"sap|abap", None),
    ("Oracle", r"oracle", None),
    ("Mainframe", r"mainframe|cobol", None),
    ("QA / testing", r"qa|sdet|tests?|testing|tester|quality assurance", None),
    ("Support", r"support", None),
    ("Embedded", r"embedded|firmware", None),
    ("Games", r"games?|gaming|unity|unreal", None),
    ("Data annotation", r"data annotation|annotation|annotator", None),
    ("Frontend", r"front[\s-]*end|ui(?:\s*/\s*ux)?\s+(?:developer|engineer)",
     r"back[\s-]*end|full[\s-]*stack|server[\s-]*side"),
)
_OFF_STACK = tuple(
    (label, re.compile(rf"(?<![a-z0-9+#])(?:{pattern})(?![a-z0-9+#])", re.I), re.compile(unless, re.I) if unless else None)
    for label, pattern, unless in OFF_STACK
)
# Database dialects of other stacks: not the candidate's SQL when deciding whether a title is theirs.
_OTHER_STACK_SQL = re.compile(r"\b(?:ms[\s-]?sql|sql\s*server|t-sql|pl[\s/-]?sql)\b", re.I)


def off_stack_term(title: str, skills: Iterable[str]) -> str | None:
    """The other-stack word in ``title`` as written (".Net", "Android"), or None when the title names
    none or also names one of the candidate's skills."""
    for _label, pattern, unless in _OFF_STACK:
        match = pattern.search(title)
        if match and not (unless and unless.search(title)):
            break
    else:
        return None
    if matching_skills(skills, [], _OTHER_STACK_SQL.sub(" ", title)):
        return None
    return match.group(0)


# ---------------------------------------------------------------------------
# Senior

_SENIOR = re.compile(
    r"(?<![a-z0-9])(?:senior|sr\.?|leads?|principals?|staff|architects?|managers?|heads?|directors?|a?vp|"
    r"vice president|distinguished)(?![a-z0-9])"
    r"|(?<![a-z0-9])(?:sde|swe|engineer|developer|level)[\s-]*(?:iii|iv|[3-9])"
    r"(?![a-z0-9+]|\.\d)(?!\s*(?:[-–]|to)\s*\d)(?!\s*\+?\s*(?:yrs?|years?|yoe)\b)"
    r"|(?<![a-z0-9])l[3-9](?![a-z0-9+])",
    re.I,
)
# Words from the senior list that don't give the role's level.
_NOT_SENIORITY = re.compile(
    r"\b(?:hiring|reporting|hr|talent acquisition|ta|recruiting|recruitment|people)\s+"
    r"(?:managers?|leads?|heads?|partners?|directors?)\b"
    r"|\bhead\s+(?:over|to|on|out|into)\b|\bheads?[\s-]+up\b|\bhead of (?:talent|hr|people|recruit\w*)\b"
    r"|\b(?:will|to|can|you(?:'|’)?ll|and|help|helps)\s+lead\b"
    r"|\b(?:with|from|by|alongside|under)\s+(?:our\s+|the\s+)?(?:senior|sr\.?|lead|staff|principal)\b"
    r"|\bmember of (?:the )?technical staff\b",
    re.I,
)


def senior_term(text: str) -> str | None:
    """The seniority word in a title or post line as written ("Senior", "Team Lead" -> "Lead",
    "SDE-3"), or None."""
    match = _SENIOR.search(_NOT_SENIORITY.sub(" ", text))
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# Skip list


def skip_list(value: str | None) -> tuple[str, ...]:
    """The ``search_skip_companies`` field as entries (empty when the field is missing)."""
    return tuple(split_list(value))


def skip_listed(names: Iterable[str | None], entries: Iterable[str]) -> str | None:
    """The first of ``names`` (company names) that contains a skip-list entry, ignoring case."""
    wanted = [entry.lower() for entry in entries if entry.strip()]
    for name in names:
        if name and any(entry in name.lower() for entry in wanted):
            return name
    return None


# ---------------------------------------------------------------------------
# Together


def company_reason(company: str) -> str:
    return f"{company} is on your skip list."


def stack_reason(term: str) -> str:
    return f'Another stack: the title says "{term}" and names none of your skills.'


def senior_reason(term: str) -> str:
    return f'Too senior: the title says "{term}".'


def title_reason(
    title: str, companies: Iterable[str | None], *, skills: Iterable[str], skip_companies: Iterable[str]
) -> tuple[str, str] | None:
    """``(key, reason)`` when the skip list or a title rule rules the job out, else None. Keys:
    ``company``, ``stack``, ``senior`` (see ``LEFT_OUT``)."""
    if company := skip_listed(companies, skip_companies):
        return "company", company_reason(company)
    if term := off_stack_term(title, skills):
        return "stack", stack_reason(term)
    if term := senior_term(title):
        return "senior", senior_reason(term)
    return None


def search_left_out(prefs: SearchPrefs, title: str, company: str | None) -> str | None:
    """At search time: the ``LEFT_OUT`` key when a job that matches otherwise isn't stored."""
    verdict = title_reason(title, [company], skills=prefs.skills, skip_companies=prefs.skip_companies)
    return verdict[0] if verdict else None


# Why a search left a matching job out, as counted in the run message.
LEFT_OUT = {"stack": "another stack", "senior": "senior", "company": "on your skip list"}


def left_out_note(counts: dict[str, int]) -> str | None:
    """``"12 left out: 7 another stack, 5 senior."``, or None when nothing was left out."""
    parts = [(counts.get(key, 0), label) for key, label in LEFT_OUT.items()]
    total = sum(n for n, _ in parts)
    if not total:
        return None
    return f"{total} left out: " + ", ".join(f"{n} {label}" for n, label in parts if n) + "."
