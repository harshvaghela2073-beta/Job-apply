"""Who gets a connection note or an email (decided by the candidate on 2026-10-27, after replies such as
"I am not a recruiter", "not considering candidates outside Vietnam", "Indonesian only" and a job
seeker asking "which company?"): only people hiring for one of the candidate's roles, in Bengaluru or
remotely from India.

- **Hiring-post authors** (``post_outreach_problem``): the post isn't a job seeker's, isn't a job
  alert or a reshare of another company's opening, names Bengaluru (or remote work in India), and
  its author is hiring: their headline shows a hiring role (recruiter, talent acquisition, HR,
  founder, engineering manager, ...) or the post says so in the first person ("we're hiring", "my
  team", "join us"). A note also needs the company (from the author's headline).
- **"Meet the hiring team"** people on job pages (``team_outreach_problem``): the job is in Bengaluru
  or remote in India, and the person's headline isn't a job seeker's.

Each problem is a short key; ``REASONS`` says it in words.
"""

import re
import unicodedata

from app.scanner.matching import city_aliases, normalize

REASONS = {
    "seeker": "a job seeker's post",
    "reshare": "a job alert or another company's opening",
    "location": "not in Bengaluru or remote in India",
    "author": "not posted by someone hiring",
    "company": "the company isn't named",
}

OUTREACH_CITY = "Bengaluru"


def plain(text: str | None) -> str:
    """NFKC-folded text ("𝐀𝐦𝐚𝐳𝐨𝐧 𝐢𝐬 #𝐇𝐢𝐫𝐢𝐧𝐠" -> "Amazon is #Hiring"), curly quotes straightened."""
    text = unicodedata.normalize("NFKC", text or "").replace("’", "'").replace("‘", "'")
    return " ".join(text.split())


_SEEKER_POST = re.compile(
    r"#?\bopen\s*to\s*work\b|#\s*(?:lookingforjob|jobseekers?|opentowork|hireme)\b|\bhire me\b|\brefer me\b"
    r"|\b(?:i'm|i am|i've been)\s+(?:currently\s+|actively\s+|now\s+)?(?:looking|seeking|searching|exploring|open)\s+"
    r"(?:for\s+|to\s+)?(?:(?!(?:developers?|engineers?|candidates?|talent|people|someone|folks|freshers)\b)[\w/&+.'-]+\s+){0,4}?"
    r"(?:jobs?|roles?|opportunit|positions?|openings?|internships?|career)"
    r"|\bactively\s+(?:looking|seeking|exploring)\s+(?:for\s+)?(?:a\s+|new\s+|my\s+next\s+)*(?:job|role|opportunit|position)"
    r"|\b(?:available|ready)\s+to\s+join\s+immediately\b|\bi(?:'m| am)\s+(?:an?\s+)?immediate\s+joiner\b"
    r"|\bserving\s+(?:my\s+)?notice\b|\b(?:my|attached)\s+(?:resume|cv)\b"
    r"|\bi(?:'m| am)\s+(?:a\s+)?(?:recent\s+|\d{4}\s+)?(?:graduate|fresher|student|passout)\b"
    r"|\b(?:\d+|one|two|three|four|five|six)\s+internships\b"
    r"|\b(?:hi|hello|dear)\s+(?:all\s+)?recruiters\b|\bconsultants?\s+available\b|\bbench\s+(?:sales|consultants?)\b",
    re.I,
)
_SEEKER_HEADLINE = re.compile(
    r"open\s*to\s*work|(?:seeking|looking\s+for|exploring)\s+(?:a\s+|an\s+|new\s+|my\s+next\s+|full[- ]time\s+)*"
    r"(?:job|role|opportunit|position|internship)|actively\s+looking|aspiring|\bstudent\b|\bintern\b|\bfresher\b"
    r"|\bgraduate\b(?!\s+(?:hiring|recruit|program))|job\s*seeker|immediate\s+joiner|available\s+immediately"
    r"|\bunemployed\b|career\s+break",
    re.I,
)
# Job alerts and reshares (2026-10-27: "Uber is hiring" shared by someone at Comviva, "#Deloitte is
# Hiring" by a Capgemini page, "Amazon is hiring ... (as claimed; verify official listing)").
_ALERT = re.compile(
    r"\bjob\s*alerts?\b|\bjobs?\s*updates?\b|\bhiring\s*alerts?\b|#\s*jobs?(?:alert|update)s?\b|#\s*jobsfor"
    r"|\bfollow\s+(?:me|us|our\s+page|@?\w+)\s+for\s+(?:more|daily|regular|latest)|\b(?:telegram|whatsapp)\s+(?:group|channel)"
    r"|\bverify\s+(?:the\s+)?official\s+listing\b|\bas\s+claimed\b|\bcomment\s+[\"'#]?\s*(?:interested|yes)\b"
    r"|\bmultiple\s+companies\b|\btop\s+companies\s+hiring\b",
    re.I,
)
_IS_HIRING = re.compile(
    r"(?:#\s*)?([A-Za-z][\w&.'-]*(?:\s+[A-Za-z][\w&.'-]*){0,3})\s+(?:is|are)\s+(?:actively\s+|now\s+|urgently\s+)?#?\s*hiring\b",
    re.I,
)
_NOT_A_COMPANY = {"we", "my team", "our team", "team", "who", "it", "this", "that", "he", "she", "they", "everyone"}
_COMPANY_LINE = re.compile(r"\bcompany\s*(?:name)?\s*[:\-–]", re.I)
_FIRST_PERSON_HIRING = re.compile(
    r"\b(?:we|i)(?:'re|'m|\s+are|\s+am)\s+(?:actively\s+|currently\s+|urgently\s+|now\s+)?"
    r"(?:hiring|looking\s+(?:for|to\s+hire)|recruiting|expanding|growing)\b"
    r"|#\s*wearehiring\b|\b(?:my|our)\s+team\b|\bjoin\s+(?:us|our|my)\b"
    r"|\bwe\s+have\s+(?:an?\s+)?(?:urgent\s+)?(?:opening|openings|vacanc\w*|positions?)\b"
    r"|\bi(?:'m| am)\s+hiring\b|\bhiring\s+for\s+(?:my|our)\b",
    re.I,
)
_HIRING_HEADLINE = re.compile(
    r"recruit\w*|\btalent\b|acquisition|\bta\b|\bhr\b|\bh\.r\b|human\s+resources?|people\s+(?:partner|operations|ops|team|success)"
    r"|\bhiring\b|staffing|\bsourc(?:er|ing)\b|headhunt\w*|founder|\bceo\b|\bcto\b|\bcoo\b|\bchief\b|\bdirector\b"
    r"|\bvp\b|vice\s+president|head\s+of|\bmanager\b|\bem\b|tech(?:nical)?\s+lead|team\s+lead|\blead\s+engineer"
    r"|engineering\s+lead|\bprincipal\b|\bpartner\b",
    re.I,
)
_REMOTE = re.compile(r"\b(?:remote|remotely|work\s+from\s+home|wfh)\b", re.I)
_INDIA = re.compile(r"\bindia\b|\bpan[\s-]india\b", re.I)


def is_seeker_post(text: str, headline: str = "") -> bool:
    return bool(_SEEKER_POST.search(plain(text)) or _SEEKER_HEADLINE.search(plain(headline)))


def _same_company(a: str, b: str) -> bool:
    a, b = normalize(a), normalize(b)
    return bool(a and b) and (a in b or b in a)


def is_reshare(text: str, company: str | None) -> bool:
    """A job alert, a list of several companies' openings, or "<Another company> is hiring"."""
    text = plain(text)
    if _ALERT.search(text) or len(_COMPANY_LINE.findall(text)) >= 2:
        return True
    for match in _IS_HIRING.finditer(text):
        named = match.group(1).strip(" #.").lower()
        words = named.split()
        while words and words[0] in ("and", "the", "so", "now", "also"):
            words = words[1:]
        named = " ".join(words)
        if not named or named in _NOT_A_COMPANY or named.endswith(("team", " we")) or words[0] in ("my", "our"):
            continue
        if not company or not _same_company(named, company):
            return True
    return False


def in_bengaluru_or_remote_india(text: str | None) -> bool:
    """Whether ``text`` (a post or a job's location) names Bengaluru, or remote work in India."""
    norm = normalize(plain(text))
    if any(re.search(rf"\b{re.escape(alias)}\b", norm) for alias in city_aliases(OUTREACH_CITY)):
        return True
    return bool(_REMOTE.search(norm) and _INDIA.search(norm))


def is_seeker_headline(headline: str | None) -> bool:
    return bool(_SEEKER_HEADLINE.search(plain(headline)))


def is_hiring_headline(headline: str | None) -> bool:
    headline = plain(headline)
    return bool(headline) and bool(_HIRING_HEADLINE.search(headline)) and not _SEEKER_HEADLINE.search(headline)


def post_outreach_problem(text: str, headline: str, company: str | None, *, need_company: bool = True) -> str | None:
    """None when a hiring post's author may get a note (``need_company``) or its address an email; else
    the reason (a ``REASONS`` key)."""
    if is_seeker_post(text, headline):
        return "seeker"
    if is_reshare(text, company):
        return "reshare"
    if not in_bengaluru_or_remote_india(text):
        return "location"
    if not (is_hiring_headline(headline) or _FIRST_PERSON_HIRING.search(plain(text))):
        return "author"
    if need_company and not company:
        return "company"
    return None


def team_outreach_problem(job_location: str | None, headline: str | None) -> str | None:
    """None when someone from a job page's "Meet the hiring team" may get a note; else the reason."""
    if not in_bengaluru_or_remote_india(job_location):
        return "location"
    if is_seeker_headline(headline):
        return "seeker"
    return None
