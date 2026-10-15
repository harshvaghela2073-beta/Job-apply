from datetime import UTC, datetime, timedelta

import pytest

from app.scanner.matching import (
    PrefsMissing,
    RoleMatcher,
    SearchPrefs,
    estimate_posted_at,
    experience_phrase,
    is_internship,
    is_today,
    load_prefs,
    match_title,
    mentions_other_city,
    normalize,
    parse_date_attr,
    parse_min_experience,
    priority,
)
from app.timeutils import local_day_bounds
from tests.factories import make_field

ROLES = (
    "Software Development Engineer, SDE1, SDE2, Java Developer, Backend Developer, Golang Developer, "
    "Full Stack Developer, Forward Deployed Engineer"
)
PREFS = SearchPrefs.from_values("1+ years, 2+ years, 3+ years", ROLES, "Bengaluru, Remote")
MATCHER = RoleMatcher(PREFS.roles)


def ten_am_today() -> datetime:
    """10:00 local time today, as UTC: "3 hours ago" is today, "11 hours ago" is yesterday."""
    midnight, _ = local_day_bounds(datetime.now(UTC))
    return midnight + timedelta(hours=10)


def test_prefs_from_values():
    assert PREFS.experience_years == (1, 2, 3)
    assert PREFS.roles[0] == "Software Development Engineer" and len(PREFS.roles) == 8
    assert PREFS.locations == ("Bengaluru", "Remote")
    assert PREFS.max_experience == 3
    with pytest.raises(PrefsMissing, match="search_roles"):
        SearchPrefs.from_values("1+ years", "", "Bengaluru")


def test_load_prefs_reads_profile_fields(session):
    make_field(session, key="search_experience_order", value="2+ years, 1+ years", position=1)
    make_field(session, key="search_roles", value="Golang Developer", position=2)
    make_field(session, key="search_locations", value="Remote", position=3)
    prefs = load_prefs(session)
    assert (prefs.experience_years, prefs.roles, prefs.locations) == ((2, 1), ("Golang Developer",), ("Remote",))


def test_load_prefs_missing(session):
    with pytest.raises(PrefsMissing):
        load_prefs(session)


def test_normalize_folds_synonyms():
    assert normalize("Back-End Engineer (SDE-II)") == "backend developer sde 2"
    assert normalize("Full Stack Dev | GoLang") == "fullstack developer golang"
    assert normalize("Software Engr I") == "software developer 1"


@pytest.mark.parametrize(
    "title, role",
    [
        ("Software Development Engineer", "Software Development Engineer"),
        ("Software Engineer", "Software Development Engineer"),
        ("SDE - Platform", "Software Development Engineer"),
        ("SDE1", "SDE1"),
        ("SDE-1", "SDE1"),
        ("SDE I (Payments)", "SDE1"),
        ("Backend Developer (SDE1)", "SDE1"),
        ("SDE II (Backend)", "SDE2"),
        ("Software Engineer 2, API Ops", "SDE2"),
        ("Software Engineer II, Google Cloud", "SDE2"),
        ("Software Engineer- 2- 4years- AI Native", "Software Development Engineer"),
        ("Java Developer", "Java Developer"),
        ("Java/Golang Developer", "Java Developer"),
        ("Back End Engineer", "Backend Developer"),
        ("Backend Engineer", "Backend Developer"),
        ("Go Developer", "Golang Developer"),
        ("GoLang Engineer", "Golang Developer"),
        ("Full-Stack Engineer", "Full Stack Developer"),
        ("Fullstack Developer", "Full Stack Developer"),
        ("Forward Deployed Engineer", "Forward Deployed Engineer"),
        ("Python Developer", None),
        ("Data Scientist", None),
        ("DEVELOPER L3", None),
        ("Business Development Executive", None),
    ],
)
def test_role_synonyms(title, role):
    match = MATCHER.match(title)
    assert (match[0] if match else None) == role


def test_role_priority_order():
    # Several roles match: the first in priority order wins.
    assert MATCHER.match("Java Backend Developer") == ("Java Developer", 3)
    assert MATCHER.match("Forward Deployed Software Engineer") == ("Software Development Engineer", 0)


@pytest.mark.parametrize(
    "title, intern",
    [("Software Engineer Intern", True), ("SDE Internship 2026", True), ("Interns - Backend", True),
     ("Internal Tools Engineer", False), ("International Payments Developer", False)],
)
def test_internship(title, intern):
    assert is_internship(title) is intern


@pytest.mark.parametrize(
    "text, years",
    [
        ("1-4 Yrs", 1),
        ("0-5 Yrs", 0),
        ("2 to 4 years", 2),
        ("Software Engineer - Backend (5-7) YOE, Core", 5),
        ("AWS Middleware Developer 3+ years exp", 3),
        ("Minimum 2 years of experience", 2),
        ("1.9+ years", 1.9),
        ("Freshers welcome", 0),
        ("SDE1: 1-3 years, SDE2: 3-5 years", 1),
        ("2026 batch, CTC 12 LPA", None),
        (None, None),
    ],
)
def test_parse_min_experience(text, years):
    assert parse_min_experience(text) == years


def test_experience_phrase():
    assert experience_phrase("Software Engineer | Java | 2 to 4 years | Bangalore") == "2 to 4 years"
    assert experience_phrase("Backend Developer") is None


def test_experience_buckets():
    assert [PREFS.experience_bucket(y) for y in (0, 1, 1.5, 2, 3)] == [0, 0, 1, 1, 2]
    assert PREFS.experience_bucket(4) is None  # needs more than 3 years
    assert PREFS.experience_bucket(None) == 2  # unknown sorts last


def test_match_title_experience_rules():
    assert match_title(PREFS, MATCHER, "Backend Developer", min_experience=1).bucket == 0
    assert match_title(PREFS, MATCHER, "Backend Developer", min_experience=3).bucket == 2
    assert match_title(PREFS, MATCHER, "Backend Developer", min_experience=4) is None
    assert match_title(PREFS, MATCHER, "Software Engineer Intern", min_experience=0) is None
    # Years in the title are used when the platform gives none.
    assert match_title(PREFS, MATCHER, "AI Backend Developer 4-7 years") is None
    assert match_title(PREFS, MATCHER, "Software Engineer | 2 to 4 years").bucket == 1
    # Without any experience figure, senior titles are skipped; the fallback bucket is used.
    assert match_title(PREFS, MATCHER, "Senior Software Engineer") is None
    assert match_title(PREFS, MATCHER, "Lead Backend Engineer") is None
    assert match_title(PREFS, MATCHER, "Golang Developer", fallback_bucket=1).bucket == 1
    # Staff/Principal/Architect are always skipped, even with a small figure.
    assert match_title(PREFS, MATCHER, "Staff Software Engineer", min_experience=0) is None
    # A senior title with an explicit small figure is kept (the platform says it fits).
    assert match_title(PREFS, MATCHER, "Senior Backend Developer", min_experience=2).bucket == 1


def test_priority_formula():
    # experience_bucket * 100 + role_index * 10 + location_index
    assert priority(0, 0, 0) == 0
    assert priority(1, 4, 0) == 140
    assert priority(2, 7, 1) == 271


def test_locations():
    assert PREFS.location_index("Bengaluru") == 0
    assert PREFS.location_index("Hybrid - Bangalore, Pune") == 0
    assert PREFS.location_index("Greater Bengaluru Area") == 0
    assert PREFS.location_index("Pune") is None
    assert PREFS.location_index("India", remote=True) == 1
    assert PREFS.location_index("Remote") == 1
    assert PREFS.location_index("Work from home") == 1
    assert mentions_other_city(PREFS.locations, "Hiring in Hyderabad and Pune")
    assert not mentions_other_city(PREFS.locations, "Hiring in Bangalore")


@pytest.mark.parametrize(
    "label, hours_ago, today",
    [
        ("Just now", 0, True),
        ("Just Now", 0, True),
        ("Few hours ago", 3, True),
        ("Today", 3, True),
        ("10 minutes ago", 10 / 60, True),
        ("3 hours ago", 3, True),
        ("an hour ago", 1, True),
        ("2h • Edited", 2, True),
        ("45m •", 0.75, True),
        ("11 hours ago", 11, False),
        ("1 day ago", 24, False),
        ("1 Day Ago", 24, False),
        ("1d •", 24, False),
        ("3+ weeks ago", 21 * 24, False),
        ("30+ days ago", 30 * 24, False),
    ],
)
def test_today_detection(label, hours_ago, today):
    now = ten_am_today()
    posted = estimate_posted_at(label, now)
    assert posted == now - timedelta(hours=hours_ago)
    assert is_today(posted, now) is today


def test_vague_labels_never_before_midnight():
    midnight, _ = local_day_bounds(datetime.now(UTC))
    early = midnight + timedelta(hours=1)
    assert estimate_posted_at("Few hours ago", early) == midnight
    assert is_today(estimate_posted_at("Few hours ago", early), early)


def test_unknown_label():
    assert estimate_posted_at("Promoted", ten_am_today()) is None
    assert estimate_posted_at(None, ten_am_today()) is None
    assert not is_today(None, ten_am_today())


def test_time_datetime_attribute():
    now = ten_am_today()
    today = now.astimezone().date().isoformat()
    yesterday = (now.astimezone().date() - timedelta(days=1)).isoformat()
    assert is_today(parse_date_attr(today), now)
    assert not is_today(parse_date_attr(yesterday), now)
    assert parse_date_attr("2026-10-13T05:00:00Z") == datetime(2026, 10, 13, 5, tzinfo=UTC)
    assert parse_date_attr("garbage") is None
