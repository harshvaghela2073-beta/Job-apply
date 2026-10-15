"""Answers for LinkedIn Easy Apply form fields, from exact sources only (docs/API_CONTRACT.md,
"LinkedIn search and Easy Apply", decided by the candidate on 2026-10-13). Nothing is guessed: a
field without a source gets no answer, and the caller discards the application when a required
field has none.

| Field                     | Source                                                              |
|---------------------------|---------------------------------------------------------------------|
| first / last / full name  | profile ``name``                                                    |
| email                     | profile ``email`` (a select must list it)                           |
| phone, phone country      | profile ``phone``; the country is India (+91)                       |
| city / location           | profile ``location``                                                |
| current CTC               | profile ``current_ctc`` ("12 LPA"): 12 in lakhs, 1200000 in rupees  |
| expected CTC              | profile ``form_expected_ctc`` ("18 LPA"), same conversion           |
| total years of experience | profile ``form_years_experience``: 1.9, or 2 for whole numbers only |
| notice period             | the "Notice period" template ("30 days"): 30 days / 1 month / the   |
|                           | option that contains it                                             |
| anything else             | a Template Library answer whose question matches the label          |
|                           | (case and punctuation ignored, >= 85% word overlap)                 |

A numeric CTC field whose label names no unit (lakhs or rupees) and a numeric notice-period field
that names no unit (days, weeks or months) are not answered from the profile: the unit would be a
guess. "Years of experience with <skill>" questions are not total experience; they need a template.

Pure functions: no database or browser access except ``AnswerBook.load``.
"""

import math
import re
from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.models import ProfileField, Template, TemplateKind
from app.scanner.matching import city_aliases

MATCH_THRESHOLD = 0.85
NOTICE_TEMPLATE = "notice period"
PHONE_COUNTRY = re.compile(r"\bindia\b|\+\s*91\b", re.I)
PLACEHOLDER_OPTIONS = ("select an option", "select", "choose", "choose an option", "please select", "--", "-")

TEXT_TYPES = ("text", "textarea", "combobox", "date")
CHOICE_TYPES = ("select", "radio")


@dataclass
class FormField:
    """One question of the current Easy Apply step, as the driver's form reader returns it."""

    id: str
    label: str
    type: str = "text"  # text | numeric | textarea | select | radio | checkbox | combobox | date
    required: bool = False
    options: tuple[str, ...] = ()
    value: str = ""  # the current value (selected option's text for select / radio)
    error: str = ""  # LinkedIn's inline error under the field, if any
    dom_id: str = ""
    checked: bool = False  # single checkbox
    raw: dict = field(default_factory=dict, repr=False)  # what the driver returned (passed back to fill)

    @classmethod
    def from_dict(cls, data: dict) -> "FormField":
        return cls(
            id=str(data.get("id", "")),
            label=clean_label(str(data.get("label") or "")),
            type=str(data.get("type") or "text"),
            required=bool(data.get("required")),
            options=tuple(str(o) for o in data.get("options") or () if str(o).strip()),
            value=str(data.get("value") or ""),
            error=" ".join(str(data.get("error") or "").split()),
            dom_id=str(data.get("dom_id") or ""),
            checked=bool(data.get("checked")),
            raw=dict(data),
        )

    @property
    def key(self) -> str:
        return f"{norm(self.label)}|{self.type}"


@dataclass(frozen=True)
class Answer:
    value: str | None  # None: no answer from any source
    reason: str  # where the value came from, or why there is none


# ---------------------------------------------------------------------------
# Text helpers


def clean_label(label: str) -> str:
    text = " ".join(label.split())
    text = re.sub(r"\s*\*+\s*$", "", text)
    text = re.sub(r"\s*\((?:required|optional)\)\s*$", "", text, flags=re.I)
    return text.strip()


def norm(text: str) -> str:
    """Lower case, punctuation removed, single spaces: "Notice Period (days)?" -> "notice period days"."""
    t = (text or "").lower().replace("’", "'").replace("'", "")
    t = re.sub(r"[^a-z0-9₹+.]+", " ", t)
    t = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", t)  # keep decimal points only
    return " ".join(t.split())


def tokens(text: str) -> set[str]:
    return set(norm(text).split())


def overlap(a: str, b: str) -> float:
    """Shared words / words of the longer text (1.0 = the same words)."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def fmt(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else f"{number:g}"


def _real_options(options: tuple[str, ...]) -> list[str]:
    return [o for o in options if norm(o) and norm(o) not in PLACEHOLDER_OPTIONS]


def choose_option(options: tuple[str, ...], answer: str) -> str | None:
    """The option equal to the answer, else the one option containing it, else the one option the
    answer starts with ("Yes, I am aligned ..." -> "Yes"). None when there is no single match."""
    wanted = norm(answer)
    if not wanted:
        return None
    candidates = [(o, norm(o)) for o in _real_options(options)]
    for test in (
        lambda n: n == wanted,
        lambda n: re.search(rf"(?<![a-z0-9]){re.escape(wanted)}(?![a-z0-9])", n) is not None,
        lambda n: wanted.startswith(n + " "),
    ):
        hits = [o for o, n in candidates if test(n)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return None
    return None


# ---------------------------------------------------------------------------
# Numbers and ranges

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WHOLE_ONLY_ERROR = re.compile(r"whole number|integer|without decimals|no decimals", re.I)
_PLAIN_NUMBER = re.compile(r"^\s*\d+(?:\.\d+)?\s*$")
_EPSILON = 1e-9


def _numbers(text: str) -> list[float]:
    return [float(m.replace(",", "")) for m in _NUMBER.findall(text or "")]


def wants_whole_number(field: FormField) -> bool:
    return bool(field.error and _WHOLE_ONLY_ERROR.search(field.error))


def _plain_number_if_rejected(field: FormField, answer: Answer) -> Answer:
    """The form rejected the answer in a text box that takes numbers only (seen live on 2026-10-14:
    "12 LPA" and "30 days" got "Invalid input"; "1.9" and "0" were accepted): the same answer as a
    plain number, with its unit dropped ("12 LPA" -> "12"). Answers without exactly one number stay."""
    if answer.value is None or not field.error or field.type not in TEXT_TYPES or field.type == "textarea":
        return answer
    if _PLAIN_NUMBER.match(answer.value):
        return answer
    numbers = _NUMBER.findall(answer.value)
    if len(numbers) != 1:
        return answer
    return Answer(numbers[0].replace(",", ""), f"{answer.reason}; as a plain number (the form rejected {answer.value!r})")


def option_range(option: str, scale: float = 1.0) -> tuple[float, float] | None:
    """The numeric range an option stands for, times ``scale``: "1-2 years" -> (1, 2),
    "More than 15 LPA" -> (15, inf), "Less than 5" -> (0, 5), "30 days" -> (30, 30)."""
    text = norm(option)
    nums = _numbers(option)  # the raw text: "10,00,000" keeps its commas
    if not nums:
        if re.search(r"\b(?:immediate|immediately|immediate joiner)\b", text):
            return (0.0, 0.0)
        return None
    low = nums[0] * scale
    if len(nums) >= 2:
        return (low, nums[1] * scale)
    # "less than 30" / "more than 30" exclude 30; "up to 30" / "30+" include it.
    if re.search(r"\b(?:less than|under|below|fewer than)\b", text):
        return (0.0, low - _EPSILON)
    if re.search(r"\b(?:upto|up to|within|max|maximum|or less)\b", text):
        return (0.0, low)
    if re.search(r"\b(?:more than|above|over|greater than)\b", text):
        return (low + _EPSILON, math.inf)
    if re.search(r"\b(?:at least|or more|plus)\b", text) or "+" in text:
        return (low, math.inf)
    return (low, low)


def pick_by_range(options: tuple[str, ...], value: float, scale_of=lambda option: 1.0, prefer_upper: bool = False) -> str | None:
    """The option whose range contains ``value``: an exact single value first, then the narrowest
    range. ``prefer_upper`` breaks a tie towards the range ending at the value ("15-30 days" over
    "30-60 days" for 30)."""
    ranges: list[tuple[str, tuple[float, float]]] = []
    for option in _real_options(options):
        scale = scale_of(option)
        if scale is None:
            continue
        found = option_range(option, scale)
        if found is not None:
            ranges.append((option, found))
    exact = [o for o, (low, high) in ranges if low == high == value]
    if len(exact) == 1:
        return exact[0]
    containing = [(o, low, high) for o, (low, high) in ranges if low <= value <= high and low != high]
    if not containing:
        return None
    width = min(high - low for _, low, high in containing)
    narrowest = [(o, low, high) for o, low, high in containing if high - low == width]
    if len(narrowest) > 1 and prefer_upper:
        narrowest = [item for item in narrowest if item[2] == value] or narrowest
    return narrowest[0][0] if len(narrowest) == 1 else None


# ---------------------------------------------------------------------------
# What a label asks for

_FIRST_NAME = re.compile(r"\b(?:first|given) name\b")
_LAST_NAME = re.compile(r"\b(?:last|family) name\b|\bsurname\b")
_FULL_NAME = re.compile(r"^(?:your )?(?:full |legal |complete )?name$|\bfull name\b|\blegal name\b")
_EMAIL = re.compile(r"\be ?mail(?: address| id)?\b")
_PHONE_COUNTRY = re.compile(r"\bcountry code\b|\bphone country\b|\bcountry\b.*\b(?:phone|mobile)\b|\b(?:phone|mobile)\b.*\bcountry\b")
_PHONE = re.compile(r"\b(?:phone|mobile|cell|contact|whatsapp) (?:phone )?(?:number|no)\b|^(?:mobile|phone|mobile phone|cell phone)$")
_CITY = re.compile(
    r"^(?:your )?(?:current )?(?:location|city)(?: city)?$|^location city$|^current (?:city|location)(?: of residence)?$"
    r"|^(?:which|what) city do you (?:currently )?live in$|^where are you (?:currently )?(?:based|located)$"
    r"|^city of residence$"
)
_SALARY = r"(?:ctc|salary|compensation|package|pay|remuneration)"
_CURRENT_CTC = re.compile(rf"\b(?:current|present|existing)\b.*\b{_SALARY}\b|\b{_SALARY}\b.*\b(?:current|present)\b")
# "exp ctc" (seen live on 2026-10-14) is the expected CTC; "years of exp" is experience.
_EXPECTED_CTC = re.compile(rf"\b(?:expected|expecting|desired|exp|expctd)\b.*\b{_SALARY}\b|\b{_SALARY} expectations?\b")
_EXPERIENCE = re.compile(
    r"\b(?:years?|yrs?)\b.*\b(?:experience|exp)\b|\b(?:experience|exp)\b.*\b(?:years?|yrs?)\b"
    r"|^(?:total |overall )?(?:work |professional )?experience$|\btotal (?:work |professional )?experience\b"
)
# "with Java", "in Python", "as a Backend Developer", "relevant": not the total.
_SKILL_SPECIFIC = re.compile(
    r"\b(?:with|in|using|on|as|for)\b(?! (?:total|years?|yrs|overall|all|months?|numbers?|decimals?)\b)\s+\S"
    r"|\brelevant\b"
)
_NOTICE = re.compile(r"\bnotice\b")
_LAKHS = re.compile(r"\b(?:lakhs?|lacs?|lpa|lac|l)\b")
_RUPEES = re.compile(r"\b(?:rupees?|inr|rs)\b|₹|\b(?:annual|annually|per annum|yearly|p a|pa)\b")
_MONTHLY = re.compile(r"\b(?:monthly|per month|a month|pm)\b")
_DAYS = re.compile(r"\bdays?\b")
_WEEKS = re.compile(r"\bweeks?\b")
_MONTHS = re.compile(r"\bmonths?\b")


def asks_total_experience(label: str) -> bool:
    text = norm(label)
    if not _EXPERIENCE.search(text):
        return False
    stripped = re.sub(r"\b(?:in|of) (?:total|years?|yrs|numbers?)\b", " ", text)
    return not _SKILL_SPECIFIC.search(stripped)


def ctc_unit(label: str) -> str | None:
    """"lakhs", "rupees" or None (not stated / monthly)."""
    text = norm(label)
    if _MONTHLY.search(text):
        return None
    if _LAKHS.search(text):
        return "lakhs"
    if _RUPEES.search(text):
        return "rupees"
    return None


def parse_lakhs(value: str | None) -> float | None:
    """"12 LPA" -> 12.0, "12,00,000" -> 12.0 (rupees are converted)."""
    nums = _numbers(value or "")
    if not nums:
        return None
    number = nums[0]
    return number / 100_000 if number >= 10_000 else number


def parse_days(value: str | None) -> int | None:
    """"30 days" -> 30, "1 month" -> 30, "2 weeks" -> 14, "Immediate" -> 0."""
    text = norm(value or "")
    if re.search(r"\bimmediate", text):
        return 0
    nums = _numbers(text)
    if not nums:
        return None
    number = nums[0]
    if _MONTHS.search(text):
        number *= 30
    elif _WEEKS.search(text):
        number *= 7
    return int(number) if float(number).is_integer() else None


def _option_days_scale(label_unit: str | None):
    def scale(option: str) -> float | None:
        text = norm(option)
        if _MONTHS.search(text):
            return 30.0
        if _WEEKS.search(text):
            return 7.0
        if _DAYS.search(text) or re.search(r"\bimmediate", text):
            return 1.0
        return {"days": 1.0, "weeks": 7.0, "months": 30.0}.get(label_unit or "")

    return scale


def _option_lakhs_scale(label_unit: str | None):
    """Options in lakhs ("10-15 LPA"), or in rupees ("₹10,00,000 - ₹15,00,000"); an option with no
    unit counts only when the label names one."""

    def scale(option: str) -> float | None:
        text = norm(option)
        nums = _numbers(option)
        if _LAKHS.search(text):
            return 1.0
        if nums and max(nums) >= 10_000:
            return 1 / 100_000
        return {"lakhs": 1.0, "rupees": 1 / 100_000}.get(label_unit or "")

    return scale


_SKILL_ALIASES = {
    "golang": ("golang", "go"),
    "spring boot": ("spring boot", "springboot", "spring"),
    "apache kafka": ("kafka",),
    "kubernetes": ("kubernetes", "k8s"),
    "gke": ("gke", "google kubernetes engine"),
    "sql": ("sql", "mysql", "postgresql", "postgres"),
}


def save_learned(session: Session, learned: list[tuple[str, str]]) -> int:
    """Add answers worked out from the profile to the Template Library (kind ``answer``), once per
    question. Returns how many were added. The caller commits and refreshes the mirror."""
    existing = {norm(t.question) for t in session.exec(select(Template).where(Template.kind == TemplateKind.answer)).all()}
    added = 0
    for question, answer in learned:
        if question and answer and norm(question) not in existing:
            session.add(Template(kind=TemplateKind.answer, question=question, answer=answer))
            existing.add(norm(question))
            added += 1
    return added


# ---------------------------------------------------------------------------
# The answer book


@dataclass
class AnswerBook:
    profile: dict[str, str]
    templates: list[tuple[str, str]]  # answered "answer" templates: (question, answer), by id
    # Answers worked out from the profile for questions no template covered (see ``_infer``); the
    # caller saves them as Template Library answers so the next form uses them directly.
    learned: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def load(cls, session: Session) -> "AnswerBook":
        profile = {f.key: f.value.strip() for f in session.exec(select(ProfileField)).all() if f.value.strip()}
        templates = [
            (t.question, t.answer.strip())
            for t in session.exec(
                select(Template).where(Template.kind == TemplateKind.answer).order_by(Template.id)
            ).all()
            if t.is_answered
        ]
        return cls(profile=profile, templates=templates)

    # -- lookups ---------------------------------------------------------------------------------

    def template_for(self, label: str) -> tuple[str, str, float] | None:
        """(question, answer, score) of the best-matching template, if it reaches the threshold."""
        best = None
        for question, answer in self.templates:
            score = overlap(label, question)
            if score >= MATCH_THRESHOLD and (best is None or score > best[2]):
                best = (question, answer, score)
        return best

    def notice_template(self) -> str | None:
        for question, answer in self.templates:
            if norm(question) == NOTICE_TEMPLATE:
                return answer
        return None

    # -- the mapping -----------------------------------------------------------------------------

    def answer(self, field: FormField) -> Answer:
        return _plain_number_if_rejected(field, self._answer(field))

    def _answer(self, field: FormField) -> Answer:
        special = self._special(field)
        if special is not None and special.value is not None:
            return special
        from_template = self._from_template(field)
        if from_template.value is not None:
            return from_template
        # A template for this question that just doesn't fit the options: never override your answer.
        template_exists = from_template.reason.startswith("no answer: the template answer")
        inferred = None if template_exists else self._infer(field)
        if inferred is not None and inferred.value is not None:
            self.learned.append((field.label, inferred.value))
            self.templates.append((field.label, inferred.value))
            return inferred
        if special is not None:
            return special
        return from_template

    # -- answers worked out from the profile (the candidate asked for this on 2026-10-14) --------

    def _skills(self) -> list[str]:
        return [s.strip() for s in self.profile.get("skills", "").split(",") if s.strip()]

    def _has_skill(self, text: str) -> bool:
        haystack = norm(text)
        for skill in self._skills():
            for variant in _SKILL_ALIASES.get(skill.lower(), (skill.lower(),)):
                if re.search(rf"(?<![a-z0-9+#]){re.escape(norm(variant))}(?![a-z0-9+#])", haystack):
                    return True
        return False

    def _yes_no(self, field: FormField, yes: bool, source: str) -> Answer | None:
        word = "Yes" if yes else "No"
        if field.type in CHOICE_TYPES:
            option = choose_option(field.options, word)
            return Answer(option, source) if option else None
        if field.type in TEXT_TYPES:
            return Answer(word, source)
        return None

    def _infer(self, field: FormField) -> Answer | None:
        label = norm(field.label)
        if not label:
            return None
        # Years with a specific skill: total experience for your skills, 0 for others.
        if _EXPERIENCE.search(label) and _SKILL_SPECIFIC.search(label):
            about_you = re.search(r"\brelevant\b|\b(?:developer|engineer|programmer|sde|software|backend|back end|full ?stack)\b", label)
            if self._has_skill(label) or about_you:
                answer = self._experience(field)
                if answer.value is not None:
                    return Answer(answer.value, "profile: your skills / role, so your total experience")
                return None
            if field.type in ("numeric", "text"):
                return Answer("0", "profile: not one of your skills")
            if field.type in CHOICE_TYPES:
                option = pick_by_range(field.options, 0.0)
                return Answer(option, "profile: not one of your skills") if option else None
            return None
        # "Do you have experience with / are you familiar with X?"
        if re.search(r"\b(?:do you have|have you|are you)\b.*\b(?:experience|worked|familiar|knowledge|proficient|hands on)\b", label):
            return self._yes_no(field, self._has_skill(label), "profile skills")
        if re.search(r"\bsponsor", label):
            return self._yes_no(field, False, "profile: works in India, no sponsorship needed")
        if re.search(r"\b(?:authori[sz]ed|authori[sz]ation|eligible|right) to work\b|\bwork (?:permit|authori[sz]ation)\b", label):
            foreign = re.search(r"\b(?:united states|usa|us|uk|united kingdom|canada|europe|eu|australia|singapore|uae|germany)\b", label)
            return self._yes_no(field, not foreign, "profile: based in India")
        if re.search(r"\b(?:commut|relocat|on ?site|onsite|hybrid|in ?office|work(?:ing)? from (?:the )?office|reside|based in|located in|live in)", label):
            city = norm(self.profile.get("location", "bengaluru"))
            other_city = re.search(r"\b(?:mumbai|pune|hyderabad|chennai|delhi|noida|gurgaon|gurugram|kolkata|ahmedabad)\b", label)
            here = city in label or "bangalore" in label or "bengaluru" in label or not other_city
            return self._yes_no(field, bool(here), "profile location and job-search locations")
        if re.search(r"\bbackground (?:check|verification)\b|\bdrug test\b", label):
            return self._yes_no(field, True, "willing")
        if re.search(r"\bbachelor|\bgraduat|\b(?:completed|have) (?:a |your )?degree\b|\bb ?e\b|\bb ?tech\b", label) and self.profile.get("graduation"):
            return self._yes_no(field, True, "profile graduation")
        if re.search(r"\bimmediate(?:ly)?\b|\bjoin (?:within|in)\b|\bstart (?:within|in)\b", label):
            notice = parse_days(self.notice_template()) or 30
            days = re.search(r"\b(\d+)\s*(day|week|month)", label)
            if "immediate" in label and not days:
                return self._yes_no(field, notice <= 0, f"notice period ({notice} days)")
            if days:
                limit = int(days.group(1)) * {"day": 1, "week": 7, "month": 30}[days.group(2)]
                return self._yes_no(field, notice <= limit, f"notice period ({notice} days)")
        if re.search(r"\b(?:gender|ethnicity|race|disab|veteran|sexual orientation|pronoun)", label) and field.type in CHOICE_TYPES:
            option = next((o for o in field.options if re.search(r"prefer not|decline|don.?t wish|do not wish|rather not", o, re.I)), None)
            return Answer(option, "voluntary question: prefer not to say") if option else None
        if re.search(r"\bhow did you (?:hear|find|learn)\b", label) and field.type in CHOICE_TYPES:
            option = next((o for o in field.options if "linkedin" in o.lower()), None)
            return Answer(option, "found on LinkedIn") if option else None
        if re.search(r"\blinkedin\b.*\b(?:url|profile|link)\b|\b(?:url|profile|link)\b.*\blinkedin\b", label):
            return self._value(field, self.profile.get("linkedin_url"), "profile LinkedIn")
        if re.search(r"\bcurrent (?:company|employer|organi[sz]ation)\b", label):
            return self._value(field, self.profile.get("current_company"), "profile current company")
        if re.search(r"\bcurrent (?:job )?(?:title|designation|role|position)\b", label):
            return self._value(field, self.profile.get("designation"), "profile designation")
        if field.type == "textarea" and re.search(r"\bcover letter\b|\bwhy\b.*\b(?:join|interested|hire|fit|apply|role)\b|\babout yourself\b", label):
            for question, answer in self.templates:
                if norm(question).startswith("why are you a good fit") and "short" in norm(question):
                    return Answer(answer, f'template "{question}"')
        return None

    def _special(self, field: FormField) -> Answer | None:
        """An answer from the profile / notice template for the fields they cover, or None when the
        label isn't one of them."""
        label = norm(field.label)
        if not label:
            return None
        name = self.profile.get("name", "")
        if _FIRST_NAME.search(label):
            return self._value(field, name.split()[0] if name else None, "profile name (first word)")
        if _LAST_NAME.search(label):
            parts = name.split()
            return self._value(field, " ".join(parts[1:]) if len(parts) > 1 else None, "profile name (after the first word)")
        if _FULL_NAME.search(label):
            return self._value(field, name or None, "profile name")
        if _PHONE_COUNTRY.search(label):
            return self._phone_country(field)
        if _EMAIL.search(label):
            return self._value(field, self.profile.get("email"), "profile email")
        if _PHONE.search(label):
            return self._value(field, self._phone(), "profile phone")
        expected = _EXPECTED_CTC.search(label)
        current = _CURRENT_CTC.search(label)
        if expected and not current:
            return self._ctc(field, "form_expected_ctc")
        if current and not expected:
            return self._ctc(field, "current_ctc")
        if asks_total_experience(field.label):
            return self._experience(field)
        if _NOTICE.search(label):
            return self._notice(field)
        if _CITY.search(label) or (field.type == "combobox" and re.search(r"\b(?:city|location)\b", label) and len(label.split()) <= 4):
            return self._city(field)
        return None

    def _value(self, field: FormField, value: str | None, source: str) -> Answer:
        """A plain profile value, fitted to the field type."""
        if not value:
            return Answer(None, f"no answer: {source} is empty")
        if field.type in CHOICE_TYPES:
            option = choose_option(field.options, value)
            if option is None:
                return Answer(None, f"no answer: {source} ({value}) isn't one of the options")
            return Answer(option, source)
        if field.type == "checkbox":
            return Answer(None, f"no answer: {source} doesn't fit a checkbox")
        return Answer(value, source)

    def _phone(self) -> str | None:
        digits = re.sub(r"\D", "", self.profile.get("phone", ""))
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        return digits or None

    def _phone_country(self, field: FormField) -> Answer:
        source = "phone country: India (+91)"
        if field.type in CHOICE_TYPES:
            hits = [o for o in _real_options(field.options) if PHONE_COUNTRY.search(o)]
            if len(hits) == 1:
                return Answer(hits[0], source)
            return Answer(None, "no answer: India (+91) isn't one of the options")
        if field.type in TEXT_TYPES:
            return Answer("+91", source)
        return Answer(None, "no answer: unexpected field type for the phone country")

    def _ctc(self, field: FormField, key: str) -> Answer:
        raw = self.profile.get(key)
        lakhs = parse_lakhs(raw)
        if lakhs is None:
            return Answer(None, f"no answer: profile {key} is empty")
        unit = ctc_unit(field.label)
        source = f"profile {key} ({raw})"
        if field.type in CHOICE_TYPES:
            option = pick_by_range(field.options, lakhs, _option_lakhs_scale(unit))
            if option is None:
                return Answer(None, f"no answer: no option contains {fmt(lakhs)} LPA ({source})")
            return Answer(option, source)
        if field.type == "numeric" or (field.type == "text" and unit is not None):
            if unit == "lakhs":
                return Answer(fmt(lakhs), f"{source} in lakhs")
            if unit == "rupees":
                return Answer(fmt(round(lakhs * 100_000)), f"{source} in rupees")
            return Answer(None, f"no answer: the field doesn't say lakhs or rupees ({source})")
        if field.type in TEXT_TYPES:
            return Answer(raw, source)
        return Answer(None, f"no answer: unexpected field type for {key}")

    def _experience(self, field: FormField) -> Answer:
        raw = self.profile.get("form_years_experience")
        nums = _numbers(raw or "")
        if not nums:
            return Answer(None, "no answer: profile form_years_experience is empty")
        decimal = nums[0]
        whole = int(nums[1]) if len(nums) > 1 and nums[1].is_integer() else int(math.floor(decimal + 0.5))
        source = f"profile form_years_experience ({raw})"
        if field.type in CHOICE_TYPES:
            option = pick_by_range(field.options, decimal)
            if option is None:
                option = pick_by_range(field.options, float(whole))
            if option is None:
                return Answer(None, f"no answer: no option contains {fmt(decimal)} years")
            return Answer(option, source)
        if field.type in ("numeric", "text", "combobox"):
            if wants_whole_number(field):
                return Answer(str(whole), f"{source}, whole number")
            return Answer(fmt(decimal), source)
        return Answer(None, "no answer: unexpected field type for years of experience")

    def _notice(self, field: FormField) -> Answer:
        raw = self.notice_template()
        if not raw:
            return Answer(None, 'no answer: the "Notice period" template is empty or missing')
        days = parse_days(raw)
        source = f'template "Notice period" ({raw})'
        label = norm(field.label)
        label_unit = "days" if _DAYS.search(label) else "weeks" if _WEEKS.search(label) else "months" if _MONTHS.search(label) else None
        if field.type in CHOICE_TYPES:
            option = choose_option(field.options, raw)
            if option is None and days is not None:
                option = pick_by_range(field.options, float(days), _option_days_scale(label_unit), prefer_upper=True)
            if option is None:
                return Answer(None, f"no answer: no option fits {raw}")
            return Answer(option, source)
        if field.type == "numeric":
            if days is None:
                return Answer(None, f"no answer: {raw!r} isn't a number of days")
            per_unit = {"days": 1, "weeks": 7, "months": 30}.get(label_unit or "")
            if per_unit is None:
                return Answer(None, f"no answer: the field doesn't say days, weeks or months ({source})")
            if days % per_unit:
                return Answer(None, f"no answer: {raw} isn't a whole number of {label_unit}")
            return Answer(str(days // per_unit), f"{source} in {label_unit}")
        if field.type in TEXT_TYPES:
            return Answer(raw, source)
        return Answer(None, "no answer: unexpected field type for the notice period")

    def _city(self, field: FormField) -> Answer:
        city = self.profile.get("location")
        if not city:
            return Answer(None, "no answer: profile location is empty")
        if field.type in CHOICE_TYPES:
            aliases = city_aliases(city)
            hits = [o for o in _real_options(field.options) if any(re.search(rf"\b{re.escape(a)}\b", norm(o)) for a in aliases)]
            if len(hits) == 1:
                return Answer(hits[0], "profile location")
            return Answer(None, f"no answer: {city} isn't one of the options")
        return self._value(field, city, "profile location")

    def _from_template(self, field: FormField) -> Answer:
        match = self.template_for(field.label)
        if match is None:
            return Answer(None, "no answer: no matching template")
        question, answer, score = match
        source = f'template "{question}"' + ("" if score >= 1 else f" ({score:.0%} match)")
        if field.type in CHOICE_TYPES:
            option = choose_option(field.options, answer)
            if option is None:
                return Answer(None, f"no answer: the template answer isn't one of the options ({source})")
            return Answer(option, source)
        if field.type == "numeric":
            if not _PLAIN_NUMBER.match(answer):
                return Answer(None, f"no answer: the template answer isn't a number ({source})")
            return Answer(answer.strip(), source)
        if field.type == "checkbox":
            if field.options:
                parts = [p for p in re.split(r"[,\n;]", answer) if p.strip()]
                chosen = [choose_option(field.options, p) for p in parts]
                if not parts or any(c is None for c in chosen):
                    return Answer(None, f"no answer: the template answer doesn't name the options ({source})")
                return Answer("\n".join(c for c in chosen if c), source)
            word = norm(answer).split(" ")[0] if norm(answer) else ""
            if word in ("yes", "true", "agree", "i", "checked"):
                return Answer("true", source)
            if word in ("no", "false"):
                return Answer("false", source)
            return Answer(None, f"no answer: the template answer isn't yes or no ({source})")
        return Answer(answer, source)
