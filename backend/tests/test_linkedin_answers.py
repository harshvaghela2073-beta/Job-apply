"""Answer mapping for LinkedIn Easy Apply (pure): every source, unit conversions, option picking,
template matching. Nothing may be guessed."""

import pytest

from app.models import TemplateKind
from app.sources.linkedin.answers import (
    AnswerBook,
    FormField,
    asks_total_experience,
    choose_option,
    overlap,
    parse_days,
    pick_by_range,
)
from tests.factories import make_field, make_template

PROFILE = {
    "name": "Harsh Vaghela",
    "email": "candidate@example.com",
    "phone": "9000000001",
    "location": "Bengaluru",
    "current_ctc": "12 LPA",
    "form_expected_ctc": "18 LPA",
    "form_years_experience": "1.9 (2 when the form only accepts whole numbers)",
    "skills": "Java, Spring Boot, Golang, Python, SQL, Redis, Apache Kafka, Kubernetes",
}
FAST_PACED = (
    "We're incredibly fast-paced. We do whatever it takes to get things done. Would you be aligned with this aspect?"
)
TEMPLATES = [
    ("Notice period", "30 days"),
    ("Expected salary (CTC)", "16-20 LPA"),
    (FAST_PACED, "Yes, I’m completely aligned with this. Even in my previous job, I worked in a similar way."),
    ("Are you comfortable commuting to this job's location?", "Yes"),
    ("Are you willing to relocate?", "Yes"),
    ("What is your highest level of education?", "Bachelor's"),
    ("Do you agree to the privacy policy?", "I agree"),
    ("What tech stack are you most familiar with?", "Java, Golang and Python."),
]
BOOK = AnswerBook(profile=PROFILE, templates=TEMPLATES)


def field(label, type="text", options=(), **extra) -> FormField:
    return FormField(id="f1", label=label, type=type, options=tuple(options), required=True, **extra)


def value(label, type="text", options=(), book=BOOK, **extra):
    return book.answer(field(label, type, options, **extra)).value


# ---------------------------------------------------------------------------
# Profile fields


def test_name_email_phone_city():
    assert value("First name") == "Harsh"
    assert value("Last name") == "Vaghela"
    assert value("Full name") == "Harsh Vaghela"
    assert value("Email address", "select", ["Select an option", "candidate@example.com"]) == "candidate@example.com"
    assert value("Email address", "select", ["someone.else@gmail.com"]) is None  # not one of LinkedIn's options
    countries = ["Select an option", "British Indian Ocean Territory (+246)", "India (+91)", "United States (+1)"]
    assert value("Phone country code", "select", countries) == "India (+91)"
    assert value("Mobile phone number") == "9000000001"
    assert value("Mobile phone number", "numeric") == "9000000001"
    other = AnswerBook(profile={**PROFILE, "phone": "+91 90000 00001"}, templates=[])
    assert value("Mobile phone number", book=other) == "9000000001"
    assert value("Location (city)", "combobox") == "Bengaluru"
    assert value("City") == "Bengaluru"
    assert value("Current city", "select", ["Mumbai", "Bangalore", "Pune"]) == "Bangalore"


def test_location_questions_are_not_the_city():
    # A question that mentions the location is not the city field: it needs a template.
    answer = BOOK.answer(field("Are you comfortable commuting to this job's location?", "radio", ["Yes", "No"]))
    assert answer.value == "Yes" and answer.reason.startswith("template")
    assert value("Preferred work location") is None


# ---------------------------------------------------------------------------
# CTC: lakhs vs rupees


@pytest.mark.parametrize(
    ("label", "kind", "expected"),
    [
        ("Current CTC (in LPA)", "numeric", "12"),
        ("What is your current CTC in lakhs?", "numeric", "12"),
        ("What is your current CTC in lakhs?", "text", "12"),
        ("Current CTC in INR", "numeric", "1200000"),
        ("Current annual CTC", "numeric", "1200000"),
        ("Current CTC (₹)", "numeric", "1200000"),
        ("Current annual CTC in Lakhs", "numeric", "12"),
        ("What is your current CTC?", "numeric", None),  # unit not stated: not guessed
        ("What is your current CTC?", "text", "12 LPA"),
        ("Current monthly salary (INR)", "numeric", None),
        ("Expected CTC (LPA)", "numeric", "18"),
        ("Expected annual CTC in INR", "numeric", "1800000"),
        ("What is your expected CTC?", "text", "18 LPA"),
        ("exp ctc", "text", "18 LPA"),  # seen live on 2026-10-14 (TCS)
        ("Current ctc", "text", "12 LPA"),
    ],
)
def test_ctc(label, kind, expected):
    assert value(label, kind) == expected


def test_ctc_options_and_reasons():
    options = ["Select an option", "0-5 LPA", "5-10 LPA", "10-15 LPA", "15+ LPA"]
    assert value("Current CTC", "select", options) == "10-15 LPA"
    assert value("Expected CTC", "radio", options) == "15+ LPA"
    rupees = ["₹0 - ₹10,00,000", "₹10,00,000 - ₹15,00,000", "₹15,00,000 - ₹25,00,000"]
    assert value("Current CTC", "select", rupees) == "₹10,00,000 - ₹15,00,000"
    assert value("Current CTC", "select", ["0-5", "5-10", "10-15"]) is None  # no unit anywhere
    assert value("Current CTC in lakhs", "select", ["0-5", "5-10", "10-15"]) == "10-15"
    answer = BOOK.answer(field("What is your current CTC?", "numeric"))
    assert answer.reason == "no answer: the field doesn't say lakhs or rupees (profile current_ctc (12 LPA))"


def test_unit_less_ctc_can_come_from_a_template():
    book = AnswerBook(profile=PROFILE, templates=[("What is your current CTC?", "12")])
    answer = book.answer(field("What is your current CTC?", "numeric"))
    assert answer.value == "12" and answer.reason == 'template "What is your current CTC?"'


# ---------------------------------------------------------------------------
# Experience


def test_total_experience_and_whole_number_fallback():
    assert value("How many years of work experience do you have?", "numeric") == "1.9"
    error = "Enter a whole number between 0 and 99"
    assert value("How many years of work experience do you have?", "numeric", error=error) == "2"
    assert value("Total years of experience", "text") == "1.9"
    assert value("Total experience (in years)", "numeric") == "1.9"
    assert value("How many years of experience do you have in total?", "numeric") == "1.9"
    assert value("Years of experience", "select", ["0-1 years", "1-2 years", "2-3 years", "3+ years"]) == "1-2 years"
    assert value("Years of experience", "select", ["1", "2", "3", "4+"]) == "2"
    assert value("Years of experience", "radio", ["Less than 1 year", "More than 5 years"]) is None


@pytest.mark.parametrize(
    "label",
    [
        "How many years of work experience do you have with Java?",
        "How many years of experience do you have in Golang?",
        "Years of experience using Kubernetes",
        "How many years of experience do you have as a Backend Developer?",
        "How many years of relevant experience do you have?",
    ],
)
def test_skill_years_come_from_the_profile(label):
    # Not "total experience" questions, but since 2026-10-14 answered from the profile: your skills,
    # your role and "relevant" experience get your total experience.
    assert not asks_total_experience(label)
    answer = AnswerBook(profile=PROFILE, templates=list(TEMPLATES)).answer(field(label, "numeric"))
    assert answer.value == "1.9"


def test_years_with_a_skill_you_dont_list_is_zero():
    answer = AnswerBook(profile=PROFILE, templates=list(TEMPLATES)).answer(field("How many years of experience do you have with Angular?", "numeric"))
    assert answer.value == "0"


def test_skill_years_come_from_a_template():
    book = AnswerBook(profile=PROFILE, templates=[("How many years of work experience do you have with Java?", "2")])
    assert value("How many years of work experience do you have with Java?", "numeric", book=book) == "2"


# ---------------------------------------------------------------------------
# Notice period


@pytest.mark.parametrize(
    ("label", "kind", "options", "expected"),
    [
        ("Notice period (in days)", "numeric", (), "30"),
        ("What is your notice period in days?", "numeric", (), "30"),
        ("Notice period in months", "numeric", (), "1"),
        ("Notice period (weeks)", "numeric", (), None),  # 30 days isn't whole weeks
        ("Notice period", "numeric", (), None),  # no unit: not guessed
        ("What is your notice period?", "text", (), "30 days"),
        ("Notice period", "select", ("Immediate", "15 days", "1 month", "2 months"), "1 month"),
        ("Notice period", "select", ("Immediate", "15 days", "30 days", "60 days"), "30 days"),
        ("Notice period", "radio", ("0-15 days", "15-30 days", "30-60 days", "60+ days"), "15-30 days"),
        ("Notice period", "radio", ("Immediate", "1-2 months", "3 months"), "1-2 months"),
        ("Notice period", "radio", ("Immediate", "Less than 30 days", "More than 30 days"), None),
    ],
)
def test_notice_period(label, kind, options, expected):
    assert value(label, kind, options) == expected


def test_notice_period_needs_its_template():
    book = AnswerBook(profile=PROFILE, templates=[])
    answer = book.answer(field("Notice period (in days)", "numeric"))
    assert answer.value is None and "Notice period" in answer.reason


# ---------------------------------------------------------------------------
# Templates


def test_template_matching_ignores_case_and_punctuation():
    assert value("ARE YOU COMFORTABLE COMMUTING TO THIS JOB’S LOCATION", "radio", ["Yes", "No"]) == "Yes"
    assert value("What tech stack are you most familiar with", "textarea") == "Java, Golang and Python."


def test_template_matching_threshold():
    relocate = "Are you willing to relocate to Bengaluru?"
    assert overlap(relocate, "Are you willing to relocate?") == pytest.approx(5 / 6)  # below 0.85
    # Below the template threshold, but the profile answers it (based in Bengaluru).
    assert value(relocate, "radio", ["Yes", "No"]) == "Yes"
    almost = FAST_PACED.replace("incredibly ", "")  # 19 of the template's 20 words
    assert overlap(almost, FAST_PACED) == pytest.approx(19 / 20)
    assert value(almost, "radio", ["Yes", "No"]) == "Yes"  # the answer starts with "Yes"


def test_template_answers_are_fitted_to_the_field():
    assert value("What is your highest level of education?", "select", ["Bachelor's Degree", "Master's Degree"]) == "Bachelor's Degree"
    book = AnswerBook(profile=PROFILE, templates=[("Can you start immediately?", "Yes")])
    assert value("Can you start immediately?", "radio", ["Yes, full time", "Yes, part time", "No"], book=book) is None
    assert value("What tech stack are you most familiar with?", "numeric") is None  # not a number
    assert value("Do you agree to the privacy policy?", "checkbox") == "true"


def test_option_helpers():
    assert choose_option(("Yes", "No"), "yes") == "Yes"
    assert choose_option(("Select an option", "Yes", "No"), "Maybe") is None
    assert pick_by_range(("0-15 days", "15-30 days", "30-60 days"), 30, prefer_upper=True) == "15-30 days"
    assert parse_days("1 month") == 30 and parse_days("2 weeks") == 14 and parse_days("Immediate") == 0


def test_load_reads_the_database(session):
    make_field(session, key="name", value="Harsh Vaghela")
    make_field(session, key="current_ctc", label="CTC", value="12 LPA", position=2)
    make_template(session, question="Notice period", answer="30 days")
    make_template(session, question="Are you willing to relocate?", answer="TBD")  # not answered yet
    make_template(session, kind=TemplateKind.email, question="Notice period email", answer="Hi")
    book = AnswerBook.load(session)
    assert book.profile == {"name": "Harsh Vaghela", "current_ctc": "12 LPA"}
    assert book.templates == [("Notice period", "30 days")]
