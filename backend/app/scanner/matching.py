"""Matching rules shared by every scanner (docs/API_CONTRACT.md, "Scans").

- The search priority comes from three profile fields, read at scan time: ``search_experience_order``,
  ``search_roles`` and ``search_locations`` (comma-separated, in priority order).
- A title matches a role with synonyms: Engineer/Developer, SDE1/SDE-1/SDE I, Golang/Go,
  Backend/Back End, Full Stack/Fullstack. "Software Development Engineer" also matches
  "Software Engineer", "Software Developer" and a bare "SDE". A title with a level ("SDE-2",
  "Software Engineer II") matches the leveled role (SDE2) when the list has one; otherwise roles are
  tried in priority order and the first match wins.
- Internships never match. The minimum experience must be at most the highest level in
  ``search_experience_order`` (3 years). When the platform gives no experience, titles with
  Senior/Sr/Lead or level III+ are skipped; Staff/Principal/Architect/Manager/Director/Head titles
  are always skipped.
- "Today" is the server's local day (Asia/Kolkata on the candidate's machine).
- ``priority = experience_bucket * 100 + role_index * 10 + location_index`` (lower is better).
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlmodel import Session, col, select

from app.models import ProfileField
from app.timeutils import local_day_bounds, local_today, to_utc

EXPERIENCE_KEY = "search_experience_order"
ROLES_KEY = "search_roles"
LOCATIONS_KEY = "search_locations"


class PrefsMissing(ValueError):
    """A search_* profile field is missing or empty."""


def split_list(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Text normalisation

_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5"}
_WORD_SYNONYMS = [
    (re.compile(r"\b(?:engineer|engineers|engg|engr|dev|developers|programmer)\b"), "developer"),
    (re.compile(r"\bgo\b"), "golang"),
    (re.compile(r"\bswe\b"), "software developer"),
]


def normalize(text: str) -> str:
    """Lower-case words separated by single spaces, with synonyms folded to one spelling, e.g.
    ``"Back-End Engineer (SDE-II)"`` -> ``"backend developer sde 2"``."""
    t = text.lower().replace("&", " and ")
    t = re.sub(r"[‐-―−]", "-", t)  # unicode dashes
    t = re.sub(r"\b(back|front)[\s-]*end\b", r"\1end", t)
    t = re.sub(r"\bfull[\s-]*stack\b", "fullstack", t)
    t = re.sub(r"\bsde[\s-]*(\d|iv|i{1,3}|v)\b", lambda m: f"sde {_ROMAN.get(m.group(1), m.group(1))}", t)
    t = re.sub(r"[^a-z0-9+#]+", " ", t)
    for pattern, replacement in _WORD_SYNONYMS:
        t = pattern.sub(replacement, t)
    # "software developer ii" -> "software developer 2"
    t = re.sub(
        r"\b(developer|sde) (iv|i{1,3}|v)\b", lambda m: f"{m.group(1)} {_ROMAN[m.group(2)]}", t
    )
    return " ".join(t.split())


# ---------------------------------------------------------------------------
# Roles

# Extra ways to say a role, as normalised phrases.
_ROLE_ALIASES = {
    "software development developer": ("software developer", "sde"),
}
_LEVELED_ROLE = re.compile(r"^sde (\d)$")
# "Software Engineer 2" is SDE2, but "Software Engineer 2-4 years" isn't.
_LEVELED_TITLE = r"\b(?:sde|software(?: development)? developer) {level}\b(?! ?\d)"


def _phrase_pattern(phrase: str) -> str:
    """Words in order, allowing up to two extra words between them ("Java Backend Developer"
    matches "java developer")."""
    words = [re.escape(word) for word in phrase.split()]
    return r"\b" + r"(?: \S+){0,2} ".join(words) + r"\b"


@dataclass(frozen=True)
class _Role:
    name: str
    index: int
    pattern: re.Pattern
    leveled: bool


class RoleMatcher:
    def __init__(self, roles: list[str] | tuple[str, ...]):
        self.roles: list[_Role] = []
        for index, name in enumerate(roles):
            norm = normalize(name)
            if not norm:
                continue
            level = _LEVELED_ROLE.match(norm)
            if level:
                pattern = _LEVELED_TITLE.format(level=level.group(1))
            else:
                phrases = (norm, *_ROLE_ALIASES.get(norm, ()))
                pattern = "|".join(f"(?:{_phrase_pattern(p)})" for p in phrases)
            self.roles.append(_Role(name, index, re.compile(pattern), bool(level)))

    def match(self, text: str) -> tuple[str, int] | None:
        """(role name, role index) of the best role ``text`` matches, or None."""
        norm = normalize(text)
        for role in sorted(self.roles, key=lambda r: (not r.leveled, r.index)):
            if role.pattern.search(norm):
                return role.name, role.index
        return None


_INTERN = re.compile(r"\b(?:intern|interns|internship|internships)\b")
_STRONG_SENIORITY = re.compile(
    r"\b(?:staff|principal|architect|director|head|vp|vice president|distinguished|fellow|manager)\b"
)
_WEAK_SENIORITY = re.compile(r"\b(?:senior|sr|lead|developer [3-9]|sde [3-9])\b")


def is_internship(text: str) -> bool:
    return bool(_INTERN.search(normalize(text)))


# ---------------------------------------------------------------------------
# Experience

_YEARS = r"(?:yrs?|years?|yoe)\b"
_NUM = r"(\d{1,2}(?:\.\d)?)"
_RANGE = re.compile(_NUM + r"\s*\+?\s*(?:-|to)\s*" + _NUM + r"\s*\+?\s*" + _YEARS, re.I)
_SINGLE = re.compile(_NUM + r"\s*\+?\s*(?:plus\s*)?" + _YEARS, re.I)
_MINIMUM = re.compile(r"\b(?:minimum|min\.?|at\s*least)\s*(?:of\s*)?" + _NUM + r"\s*" + _YEARS, re.I)
_FRESHER = re.compile(r"\bfreshers?\b", re.I)


def parse_min_experience(text: str | None) -> float | None:
    """The smallest minimum experience in ``text`` ("1-4 Yrs" -> 1, "(5-7) YOE" -> 5,
    "3+ years" -> 3, "Fresher" -> 0), or None when it says nothing about experience."""
    if not text:
        return None
    t = re.sub(r"[‐-―()]", lambda m: "-" if m.group() not in "()" else " ", text)
    values: list[float] = []
    for match in _RANGE.finditer(t):
        values.append(float(match.group(1)))
    t_without_ranges = _RANGE.sub(" ", t)
    for pattern in (_MINIMUM, _SINGLE):
        for match in pattern.finditer(t_without_ranges):
            values.append(float(match.group(1)))
    values = [v for v in values if v <= 30]
    if _FRESHER.search(t):
        values.append(0.0)
    return min(values) if values else None


def experience_phrase(text: str | None) -> str | None:
    """The first experience figure in ``text`` as written, e.g. ``"2 to 4 years"``."""
    if not text:
        return None
    for pattern in (_RANGE, _MINIMUM, _SINGLE):
        match = pattern.search(text)
        if match:
            return " ".join(match.group().split())
    return None


def experience_level_years(label: str) -> int | None:
    """``"2+ years"`` -> 2, ``"Fresher"`` -> 0."""
    match = re.search(r"\d+", label)
    if match:
        return int(match.group())
    return 0 if _FRESHER.search(label) else None


# ---------------------------------------------------------------------------
# Locations

_CITY_ALIASES = {
    "bengaluru": ("bengaluru", "bangalore", "bengalore", "blr"),
    "mumbai": ("mumbai", "bombay", "navi mumbai"),
    "delhi": ("delhi", "new delhi", "delhi ncr", "ncr"),
    "gurugram": ("gurugram", "gurgaon"),
    "noida": ("noida",),
    "hyderabad": ("hyderabad", "secunderabad"),
    "chennai": ("chennai", "madras"),
    "pune": ("pune",),
    "kolkata": ("kolkata", "calcutta"),
    "ahmedabad": ("ahmedabad",),
    "kochi": ("kochi", "cochin"),
    "jaipur": ("jaipur",),
    "indore": ("indore",),
    "chandigarh": ("chandigarh", "mohali"),
    "coimbatore": ("coimbatore",),
    "trivandrum": ("trivandrum", "thiruvananthapuram"),
}
_REMOTE_NAMES = {"remote", "work from home", "wfh", "anywhere"}
_REMOTE_TEXT = re.compile(r"\b(?:remote|work from home|wfh|remotely)\b")


def is_remote_location(name: str) -> bool:
    return normalize(name) in _REMOTE_NAMES


def city_aliases(name: str) -> tuple[str, ...]:
    norm = normalize(name)
    for city, aliases in _CITY_ALIASES.items():
        if norm == city or norm in aliases:
            return aliases
    return (norm,)


def _mentions(norm_text: str, phrase: str) -> bool:
    return re.search(rf"\b{re.escape(phrase)}\b", norm_text) is not None


def location_index(locations: tuple[str, ...], text: str | None, *, remote: bool = False) -> int | None:
    """Position of the first preferred location that ``text`` is in (or that a remote job fits)."""
    norm = normalize(text or "")
    for index, name in enumerate(locations):
        if is_remote_location(name):
            if remote or _REMOTE_TEXT.search(norm):
                return index
        elif any(_mentions(norm, alias) for alias in city_aliases(name)):
            return index
    return None


def mentions_other_city(locations: tuple[str, ...], text: str) -> bool:
    """Whether ``text`` names a known Indian city that isn't one of ``locations``."""
    norm = normalize(text)
    wanted = {alias for name in locations for alias in city_aliases(name)}
    return any(
        _mentions(norm, alias)
        for aliases in _CITY_ALIASES.values()
        if not wanted.intersection(aliases)
        for alias in aliases
    )


# ---------------------------------------------------------------------------
# Search preferences


@dataclass(frozen=True)
class SearchPrefs:
    experience: tuple[str, ...]  # as written, e.g. ("1+ years", "2+ years", "3+ years")
    experience_years: tuple[int, ...]  # (1, 2, 3)
    roles: tuple[str, ...]
    locations: tuple[str, ...]

    @classmethod
    def from_values(cls, experience: str, roles: str, locations: str) -> "SearchPrefs":
        labels = tuple(split_list(experience))
        years = tuple(y for y in (experience_level_years(label) for label in labels) if y is not None)
        prefs = cls(labels, years, tuple(split_list(roles)), tuple(split_list(locations)))
        for key, value in (
            (EXPERIENCE_KEY, prefs.experience_years),
            (ROLES_KEY, prefs.roles),
            (LOCATIONS_KEY, prefs.locations),
        ):
            if not value:
                raise PrefsMissing(f"The profile field {key!r} is empty, so there is nothing to search for.")
        return prefs

    @property
    def max_experience(self) -> int:
        """The highest minimum experience a job may ask for."""
        return max(self.experience_years)

    def experience_bucket(self, min_years: float | None) -> int | None:
        """Position in ``search_experience_order`` of the level a job fits: the smallest level that
        is at least the job's minimum. Unknown experience sorts last; None means it needs more
        experience than any level."""
        if min_years is None:
            return len(self.experience_years) - 1
        fitting = [years for years in self.experience_years if years >= min_years]
        if not fitting:
            return None
        return self.experience_years.index(min(fitting))

    def location_index(self, text: str | None, *, remote: bool = False) -> int | None:
        return location_index(self.locations, text, remote=remote)


def load_prefs(session: Session) -> SearchPrefs:
    rows = session.exec(
        select(ProfileField).where(col(ProfileField.key).in_([EXPERIENCE_KEY, ROLES_KEY, LOCATIONS_KEY]))
    ).all()
    values = {row.key: row.value for row in rows}
    return SearchPrefs.from_values(
        values.get(EXPERIENCE_KEY, ""), values.get(ROLES_KEY, ""), values.get(LOCATIONS_KEY, "")
    )


@dataclass(frozen=True)
class TitleMatch:
    role: str
    role_index: int
    bucket: int
    min_experience: float | None


def match_title(
    prefs: SearchPrefs,
    matcher: RoleMatcher,
    title: str,
    *,
    min_experience: float | None = None,
    fallback_bucket: int | None = None,
) -> TitleMatch | None:
    """Apply the role, internship and experience rules to a job title.

    ``min_experience`` is the platform's own figure; without it, a figure in the title
    ("2 to 4 years") is used, and without that, ``fallback_bucket`` (e.g. from LinkedIn's
    experience-level filter) or the last bucket.
    """
    if is_internship(title):
        return None
    role = matcher.match(title)
    if role is None:
        return None
    norm = normalize(title)
    if _STRONG_SENIORITY.search(norm):
        return None
    if min_experience is None:
        min_experience = parse_min_experience(title)
    if min_experience is None:
        if _WEAK_SENIORITY.search(norm):
            return None
        bucket = fallback_bucket if fallback_bucket is not None else len(prefs.experience_years) - 1
    else:
        bucket = prefs.experience_bucket(min_experience)
        if bucket is None:
            return None
    return TitleMatch(role[0], role[1], bucket, min_experience)


def priority(bucket: int, role_index: int, location_idx: int) -> int:
    return bucket * 100 + role_index * 10 + location_idx


# ---------------------------------------------------------------------------
# Dates

_JUST_NOW = re.compile(r"\b(?:just now|moments? ago|now|few seconds? ago|seconds? ago)\b")
_FEW_MINUTES = re.compile(r"\bfew minutes? ago\b")
_VAGUE_TODAY = re.compile(r"\b(?:few hours? ago|today)\b")
_ONE_UNIT = re.compile(r"\ban?\s+(?=(?:second|minute|hour|day|week|month|year)\b)")
_YESTERDAY = re.compile(r"\byesterday\b")
_RELATIVE = re.compile(
    r"(\d+)\s*\+?\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?|yrs?|mo|m|h|d|w|y)\b"
)
_UNIT_DELTAS = {
    "s": timedelta(seconds=1),
    "mi": timedelta(minutes=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
    "w": timedelta(weeks=1),
    "mo": timedelta(days=30),
    "y": timedelta(days=365),
}


def _unit_delta(unit: str) -> timedelta:
    if unit.startswith("mo"):
        return _UNIT_DELTAS["mo"]
    if unit.startswith("min") or unit == "m":
        return _UNIT_DELTAS["m"]
    return _UNIT_DELTAS[unit[0]]


def estimate_posted_at(label: str | None, now: datetime) -> datetime | None:
    """When a platform's label ("Just now", "Few hours ago", "3 hours ago", "1 Day Ago", "2h",
    "30+ days ago") says the item was posted, as UTC. Vague labels for today ("Few hours ago",
    "Today") become 3 hours ago, but never before local midnight. None if the label isn't
    understood."""
    if not label:
        return None
    text = _ONE_UNIT.sub("1 ", " ".join(label.lower().split()))  # "an hour ago" -> "1 hour ago"
    now = to_utc(now)
    if _FEW_MINUTES.search(text):
        return now - timedelta(minutes=5)
    if _VAGUE_TODAY.search(text):
        midnight, _ = local_day_bounds(now)
        return max(now - timedelta(hours=3), midnight)
    if _YESTERDAY.search(text):
        return now - timedelta(days=1)
    match = _RELATIVE.search(text)
    if match:
        return now - int(match.group(1)) * _unit_delta(match.group(2))
    if _JUST_NOW.search(text):
        return now
    return None


def parse_date_attr(value: str | None) -> datetime | None:
    """A ``<time datetime=...>`` value as UTC. A plain date means local midnight of that day."""
    if not value:
        return None
    value = value.strip()
    try:
        if len(value) == 10:
            day = date.fromisoformat(value)
            return datetime(day.year, day.month, day.day).astimezone().astimezone(UTC)
        return to_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def is_today(moment: datetime | None, now: datetime) -> bool:
    return moment is not None and local_today(to_utc(moment)) == local_today(to_utc(now))
