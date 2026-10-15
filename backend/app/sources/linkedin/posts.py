"""``linkedin_posts``: LinkedIn hiring posts that have a Google Form link or an email address. Needs
the saved login; skipped when LinkedIn isn't connected.

- Searches, newest first (``post_queries``): 5 aimed at Google Forms (``FORM_QUERIES``, the past
  week, read further with more scrolls), then one ``hiring <role>`` search per role (the past 24
  hours). Up to ``POST_MAX_PAGE_LOADS`` page loads per run.
- A post matches when it isn't a course or training advert, a line that isn't about an internship
  names one of the roles, any experience it states starts at 3 years or less, its location (if it
  names one) is a preferred location, and it was posted today - or, with a Google Form, within the
  past 7 days (the candidate's choice on 2026-10-15).
- Google Form links are found in the text and link targets; ``lnkd.in`` short links of posts that
  match otherwise are followed with a plain request (``resolve_link``, up to ``MAX_SHORT_LINKS`` per
  run, posts that mention a form first) to see whether they lead to a Google Form.
- The run message counts the posts read, those with a form or an email, and why matching ones were
  left out (``LEFT_OUT``); each Google Form post's decision is logged.
- A login wall or security check marks LinkedIn as disconnected and stops the run.
- The post's author (name and profile link, when a person posted it) is stored on the job: they get
  a connection note from ``linkedin_outreach`` while that switch is on.
"""

import logging
import re
from datetime import datetime, timedelta
from urllib.parse import quote, unquote, urlencode

from app.browser import BrowserSession, BrowserUnavailable
from app.models import ApplyType, Platform, ScanKind, ScanStatus
from app.scanner.base import (
    Candidate,
    capture_failure,
    LoginWall,
    Pacer,
    ScanBlocked,
    ScanContext,
    ScanOutcome,
    ScanReport,
    truncate,
)
from app.scanner.matching import (
    RoleMatcher,
    estimate_posted_at,
    experience_phrase,
    is_internship,
    is_today,
    mentions_other_city,
    parse_min_experience,
    priority,
)
from app.scanner.store import finish_scan
from app.timeutils import to_utc
from app.sources.linkedin import selectors as S
from app.sources.linkedin.jobs import LOCAL_TZ, next_search_after, search_interval
from app.sources.linkedin.login import LINKEDIN_LOGIN
from app.sources.linkedin.parse import (
    Post,
    company_from_headline,
    find_emails,
    find_form_links,
    find_short_links,
    parse_posts,
)

logger = logging.getLogger(__name__)

NOT_CONNECTED = "LinkedIn isn't connected, so hiring posts weren't searched. Connect LinkedIn to include them."
LOGIN_EXPIRED = "The saved LinkedIn login has expired. Connect again."
SECURITY_CHECK = "LinkedIn asked for a security check. Connect again and complete it in the window."
_FORM_WORDS = re.compile(r"\b(?:google\s*form|form\s*link|fill\s+(?:out\s+)?(?:this|the)\s+form|apply\s+here|forms?)\b", re.I)
_LINE_SPLIT = re.compile(r"[\n•|;]+|(?<=[.!?])\s+")


# Searches aimed at posts with a Google Form, read further (more scrolls) than the role searches.
# 2026-10-15: one such search found about two form posts a day; the candidate asked for more.
FORM_QUERIES = (
    'hiring "google form"',
    '"google form" developer',
    '"google form" "software engineer"',
    '"google form" SDE',
    '"fill the form" hiring',
)

# Why a post with a Google Form or email didn't become a job (counted in the run message).
LEFT_OUT = {
    "course": "a course or training ad",
    "date": "too old (emails: today only; forms: 7 days)",
    "role": "none of your roles",
    "experience": "more experience than yours",
    "city": "another city",
}


def post_queries(roles: tuple[str, ...]) -> list[str]:
    """Short searches, the way a person types them. Long nested boolean queries made LinkedIn answer
    "No results found" on 2026-10-13; the role, experience and location are checked on each post."""
    # One role per search: "hiring A OR B" made LinkedIn return any post naming B (2026-10-14), and it
    # doesn't index link text such as "forms.gle".
    return list(FORM_QUERIES) + [f'hiring "{role}"' if " " in role else f"hiring {role}" for role in roles]


# A course / training-programme advert, not a job (2026-10-15: "Learn frontend technologies like HTML,
# CSS ... Google form: ..."). Two or more of these signs.
_COURSE_SIGNS = (
    re.compile(r"\b(?:learn|you(?:'|’)?ll learn|master)\b(?! (?:more|about (?:us|the role)))", re.I),
    re.compile(r"\b(?:course|bootcamp|boot camp|workshop|webinar|masterclass|certification|training program(?:me)?|cohort)\b", re.I),
    re.compile(r"\b(?:enrol(?:l)?|enrol(?:l)?ment|register now|seats? (?:left|limited)|limited seats|batch starts?|fees?|placement assistance)\b", re.I),
    re.compile(r"\b(?:build real[- ]world projects|strengthen your portfolio|hands[- ]on projects|mentorship)\b", re.I),
)


def is_course_ad(text: str) -> bool:
    return sum(1 for sign in _COURSE_SIGNS if sign.search(text)) >= 2


def _emails(post: Post) -> list[str]:
    return find_emails(post.text, *(link.removeprefix("mailto:") for link in post.links if link.startswith("mailto:")))


def search_url(query: str) -> str:
    """The content search for ``query``: the past 24 hours, or the past week for a Google Form search."""
    filters = [S.CONTENT_SEARCH_PAST_WEEK if name == "datePosted" and query in FORM_QUERIES else (name, value)
               for name, value in S.CONTENT_SEARCH_FILTERS]
    return f"{S.CONTENT_SEARCH_URL}?{urlencode([('keywords', query), *filters], quote_via=quote)}"


def recent_enough(posted: datetime | None, now: datetime, *, has_form: bool) -> bool:
    """Posted today, or for a post with a Google Form within ``FORM_POST_MAX_AGE_DAYS`` (2026-10-15).
    An unknown date counts: the searches cover only that window."""
    if posted is None:
        return True
    if has_form:
        return to_utc(now) - to_utc(posted) <= timedelta(days=S.FORM_POST_MAX_AGE_DAYS)
    return is_today(posted, now)


def best_role(matcher: RoleMatcher, text: str) -> tuple[str, int] | None:
    """The most important role named on a line that isn't about an internship."""
    best = None
    for line in _LINE_SPLIT.split(text):
        if not line.strip() or is_internship(line):
            continue
        match = matcher.match(line)
        if match is not None and (best is None or match[1] < best[1]):
            best = match
    return best


class LinkedInPostsScanner:
    kind = ScanKind.linkedin_posts
    platform = Platform.linkedin

    def run(self, ctx: ScanContext) -> ScanOutcome:
        due = next_search_after(ctx.engine, ctx.now, search_interval(ctx), kind=ScanKind.linkedin_posts)
        if due is not None:
            return ScanOutcome(
                status=ScanStatus.skipped, message=f"Next post search after {due.astimezone(LOCAL_TZ):%H:%M}."
            )
        ctx.connections.sync_from_chrome(self.platform)
        if not ctx.connections.login_usable(self.platform):
            return ScanOutcome(status=ScanStatus.skipped, message=NOT_CONNECTED)
        profile_dir = ctx.browsers.profile_dir(self.platform)

        def task(driver) -> ScanReport | None:
            browser = driver.open(profile_dir=profile_dir, headless=ctx.settings.scan_headless)
            try:
                # The login check before the scan: no li_at cookie means logged out (no page load).
                if not LINKEDIN_LOGIN.has_auth_cookie(browser.cookies()):
                    return None
                return self.scan(browser, ctx)
            finally:
                browser.close()

        report = ctx.browsers.worker(self.platform.value).submit(task).result()
        if report is None:
            ctx.connections.record_login_lost(self.platform, LOGIN_EXPIRED)
            return ScanOutcome(status=ScanStatus.skipped, message=f"{LOGIN_EXPIRED} Hiring posts weren't searched.")
        return finish_scan(ctx, self.platform, report)

    def scan(self, browser: BrowserSession, ctx: ScanContext) -> ScanReport:
        report = ScanReport()
        pacer = Pacer(S.POST_MAX_PAGE_LOADS, ctx.wait_range, ctx.stop)
        matcher = RoleMatcher(ctx.prefs.roles)
        posts: dict[str, Post] = {}
        empty_shot_taken = False
        try:
            for query in post_queries(ctx.prefs.roles):
                if not pacer.next_load():
                    break
                result = browser.goto(search_url(query), wait_for=S.POST_WAIT, timeout=60)
                url = result.url.lower()
                if any(marker in url for marker in S.SECURITY_CHECK_MARKERS):
                    report.login_lost = SECURITY_CHECK
                    raise LoginWall("LinkedIn showed a security check; stopped.")
                if LINKEDIN_LOGIN.is_login_wall(result.url):
                    report.login_lost = LOGIN_EXPIRED
                    raise LoginWall("LinkedIn redirected to its login page; stopped.")
                report.login_ok = True
                # LinkedIn renders about three posts at a time: read before and after each scroll.
                html = browser.html()
                page_posts = parse_posts(html)
                for _ in range(S.POST_FORM_SCROLLS if query in FORM_QUERIES else S.POST_SCROLLS):
                    if not pacer.next_load():
                        break
                    browser.scroll()
                    more = parse_posts(browser.html())
                    known = {p.urn for p in page_posts}
                    page_posts += [p for p in more if p.urn not in known]
                if not page_posts and "no results found" not in html.lower():
                    # Usually a changed page layout or a throttled, half-loaded page, not "no posts today".
                    raise ScanBlocked("LinkedIn's post search page showed no readable posts.")
                if not page_posts and not empty_shot_taken:
                    # One screenshot per run of a search that read no posts, to tell "none today" from
                    # "the page shows posts we can't read".
                    empty_shot_taken = True
                    report.notes.append(f'"{query}" read no posts.' + capture_failure(browser, self.kind.value))
                for post in page_posts:
                    posts.setdefault(post.urn, post)
            followed = 0
            behind_short_links = 0
            left_out: dict[str, int] = {}
            # Posts that mention a form first: their short links are the likeliest to hide one.
            for post in sorted(posts.values(), key=lambda p: not _FORM_WORDS.search(p.text)):
                candidate, reason = self.judge(post, ctx, matcher)
                if candidate is None and reason == "contact" and followed < S.MAX_SHORT_LINKS:
                    candidate, opened = self._try_short_links(browser, post, ctx, matcher, S.MAX_SHORT_LINKS - followed)
                    followed += opened
                    behind_short_links += candidate is not None
                if candidate is not None:
                    report.candidates.append(candidate)
                elif reason != "contact":
                    left_out[reason] = left_out.get(reason, 0) + 1
                if find_form_links(post.text, *post.links) or _FORM_WORDS.search(post.text):
                    why = reason if reason != "contact" else self.judge(
                        post, ctx, matcher, require_contact=False, assume_form=True
                    )[1]
                    logger.info(
                        "Form post %s: %s; form link %s, %d lnkd.in link(s) | %s", post.urn,
                        "matched" if candidate is not None else LEFT_OUT.get(why, why or "no form or email found"),
                        "yes" if find_form_links(post.text, *post.links) else "no",
                        len(find_short_links(post.text, *post.links)), truncate(post.text, 160),
                    )
            forms = sum(1 for post in posts.values() if find_form_links(post.text, *post.links))
            emails = sum(1 for post in posts.values() if not find_form_links(post.text, *post.links) and _emails(post))
            note = (
                f"Read {len(posts)} posts from today's searches; {forms} had a Google Form"
                + (f" ({behind_short_links} behind lnkd.in links)" if behind_short_links else "")
                + f", {emails} an email; {len(report.candidates)} matched your roles, experience and locations."
            )
            if left_out:
                note += " Left out: " + ", ".join(f"{n} {LEFT_OUT[r]}" for r, n in left_out.items()) + "."
            if followed:
                note += f" Followed {followed} lnkd.in link(s)."
            report.notes.append(note)
        except BrowserUnavailable as error:
            report.failure = str(error)
        except ScanBlocked as error:
            # A login wall is self-explanatory; other blocks get a screenshot of what was on screen.
            note = "" if isinstance(error, LoginWall) else capture_failure(browser, self.kind.value)
            report.failure = str(error) + note
        except Exception as error:  # noqa: BLE001 - keep what was found so far
            logger.exception("LinkedIn post scan stopped")
            text = str(error).strip()
            report.failure = (
                f"The scan stopped: {text.splitlines()[0] if text else type(error).__name__}"
                + capture_failure(browser, self.kind.value)
            )
        report.page_loads = pacer.loads
        return report

    def _try_short_links(self, browser, post: Post, ctx, matcher, budget: int) -> tuple[Candidate | None, int]:
        """Follow a post's lnkd.in links with a plain request each (no page load) to find a Google Form
        behind them, for posts that match otherwise (role, experience, location, today)."""
        # Judged as a form post (7 days), since a form is what the links are followed for: with the
        # email rule (today only) the week's form posts were dropped before their links were opened.
        if budget <= 0 or self.judge(post, ctx, matcher, require_contact=False, assume_form=True)[0] is None:
            return None, 0
        opened = 0
        for link in find_short_links(post.text, *post.links)[: min(2, budget)]:
            opened += 1
            try:
                response = browser.resolve_link(link)
            except Exception as error:  # noqa: BLE001 - one bad link doesn't stop the scan
                logger.info("Following %s failed: %s", link, error)
                continue
            forms = find_form_links(unquote(response.url), response.text)
            if forms:
                post.links.append(forms[0])
                return self.judge(post, ctx, matcher)[0], opened
        return None, opened

    def candidate(
        self, post: Post, ctx: ScanContext, matcher: RoleMatcher, *, require_contact: bool = True
    ) -> Candidate | None:
        return self.judge(post, ctx, matcher, require_contact=require_contact)[0]

    def judge(
        self, post: Post, ctx: ScanContext, matcher: RoleMatcher, *, require_contact: bool = True,
        assume_form: bool = False,
    ) -> tuple[Candidate | None, str]:
        """The job for a post, or None and why not: "contact" (no form or email) or a ``LEFT_OUT`` key.
        ``assume_form``: date it like a Google Form post (7 days) before its short links are followed."""
        prefs = ctx.prefs
        forms = find_form_links(post.text, *post.links)
        emails = _emails(post)
        if require_contact and not forms and not emails:
            return None, "contact"
        if is_course_ad(post.text):
            return None, "course"
        posted = estimate_posted_at(post.time_label, ctx.now)
        if not recent_enough(posted, ctx.now, has_form=bool(forms) or assume_form):
            return None, "date"
        role = best_role(matcher, post.text)
        if role is None:
            return None, "role"
        min_experience = parse_min_experience(post.text)
        bucket = prefs.experience_bucket(min_experience)
        if bucket is None:
            return None, "experience"
        location_index = prefs.location_index(post.text)
        location = ""
        if location_index is None:
            if mentions_other_city(prefs.locations, post.text):
                return None, "city"
            location_index = len(prefs.locations) - 1  # no location named
        else:
            location = prefs.locations[location_index]
        company = company_from_headline(post.headline) or post.author or "LinkedIn post"
        return Candidate(
            platform=self.platform,
            external_id=post.urn,
            title=f"{role[0]} (hiring post)",
            company=company,
            url=post.url,
            post_url=post.url,
            apply_type=ApplyType.google_form if forms else ApplyType.email,
            form_url=forms[0] if forms else None,
            contact_email=emails[0] if emails else None,
            priority=priority(bucket, role[1], location_index),
            matched_role=role[0],
            location=location,
            short_description=truncate(post.text),
            posted_at=posted,
            experience=experience_phrase(post.text),
            # The author gets a connection note later (outreach.py), when it's a person.
            contact_name=(post.author or None) if post.author_url else None,
            contact_profile_url=post.author_url,
        ), ""
