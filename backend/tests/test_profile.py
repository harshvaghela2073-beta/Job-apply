import pytest
from sqlmodel import select

from app.models import ProfileField
from tests.conftest import FAKE_PDF
from tests.factories import make_field

EMPTY_SUMMARY = {
    "name": None,
    "email": None,
    "phone": None,
    "location": None,
    "current_company": None,
    "designation": None,
    "total_experience": None,
    "current_ctc": None,
    "graduation": None,
    "linkedin_url": None,
    "naukri_url": None,
    "skills": [],
    "resume": {"filename": "Test_Resume.pdf", "url": "/api/profile/resume"},
}


def field(key, value="", section="Personal", label=None, **extra):
    return {"key": key, "label": label or key.replace("_", " ").title(), "section": section,
            "value": value, **extra}


def put(client, *fields):
    response = client.put("/api/profile/fields", json={"fields": list(fields)})
    assert response.status_code == 200, response.text
    return response.json()


def positions(profile) -> dict[str, int]:
    return {f["key"]: f["position"] for s in profile["sections"] for f in s["fields"]}


def test_empty_profile(client):
    response = client.get("/api/profile")
    assert response.status_code == 200
    assert response.json() == {"summary": EMPTY_SUMMARY, "sections": []}


def test_summary_from_keys(client):
    profile = put(
        client,
        field("name", "Asha Rao"),
        field("email", "asha@example.com"),
        field("phone", "   "),  # empty -> null
        field("current_company", "Acme | Payments", section="Current job"),
        field("total_experience", "5 years", section="Current job"),
        field("skills", " Java, Spring Boot ,, Golang , ", section="Skills"),
        field("twitter", "@asha", section="Personal"),  # not part of the summary
    )
    assert profile == client.get("/api/profile").json()
    assert profile["summary"] == {
        **EMPTY_SUMMARY,
        "name": "Asha Rao",
        "email": "asha@example.com",
        "current_company": "Acme | Payments",
        "total_experience": "5 years",
        "skills": ["Java", "Spring Boot", "Golang"],
    }


def test_sections_grouping_and_order(client, session):
    # Inserted out of order; positions decide.
    make_field(session, key="skills", label="Skills", section="Skills", value="Go", position=30)
    make_field(session, key="current_company", label="Company", section="Current job", value="Acme", position=12)
    make_field(session, key="email", label="Email", section="Personal", value="a@x.com", position=2)
    make_field(session, key="designation", label="Role", section="Current job", value="SDE", position=10)
    make_field(session, key="name", label="Name", section="Personal", value="Asha", position=1)
    make_field(session, key="extra", label="Extra", section="Personal", value="", position=40)

    sections = client.get("/api/profile").json()["sections"]
    assert [s["title"] for s in sections] == ["Personal", "Current job", "Skills"]
    assert [f["key"] for f in sections[0]["fields"]] == ["name", "email", "extra"]
    assert [f["key"] for f in sections[1]["fields"]] == ["designation", "current_company"]
    assert sections[1]["fields"][1] == {
        "key": "current_company", "label": "Company", "section": "Current job", "value": "Acme", "position": 12,
    }


def test_upsert_positions(client, session):
    profile = put(client, field("name", "Asha"), field("email", "a@x.com"))
    assert positions(profile) == {"name": 1, "email": 2}  # new fields: max + 1, in request order

    # Explicit position for a new field; update without position keeps it.
    profile = put(client, field("phone", "123", position=10), field("name", "Asha Rao", label="Full name"))
    assert positions(profile) == {"name": 1, "email": 2, "phone": 10}
    name = profile["sections"][0]["fields"][0]
    assert name == {"key": "name", "label": "Full name", "section": "Personal", "value": "Asha Rao", "position": 1}

    # A new field without position goes after the current max.
    profile = put(client, field("location", "Pune"))
    assert positions(profile)["location"] == 11

    # An explicit position moves an existing field; the section can change too.
    profile = put(client, field("email", "a@x.com", section="Contact", position=0))
    assert positions(profile)["email"] == 0
    assert [s["title"] for s in profile["sections"]] == ["Contact", "Personal"]

    assert len(session.exec(select(ProfileField)).all()) == 4


def test_value_keeps_line_breaks(client):
    profile = put(client, field("address", "Line 1\r\nLine 2\n"))
    assert profile["sections"][0]["fields"][0]["value"] == "Line 1\nLine 2"


@pytest.mark.parametrize("key", ["Name", "1st", "has-dash", "has space", "", "_x", "naïve", "abc\n"])
def test_invalid_key_is_422(client, key):
    response = client.put("/api/profile/fields", json={"fields": [field("ok_key", "x"), field(key, "x")]})
    assert response.status_code == 422
    assert client.get("/api/profile").json()["sections"] == []  # nothing written


@pytest.mark.parametrize("missing", ["key", "label", "section", "value"])
def test_missing_or_blank_parts_are_422(client, missing):
    body = field("name", "Asha")
    del body[missing]
    assert client.put("/api/profile/fields", json={"fields": [body]}).status_code == 422
    if missing in ("label", "section"):
        body = field("name", "Asha", **{missing: "  "})
        assert client.put("/api/profile/fields", json={"fields": [body]}).status_code == 422


def test_delete_field(client):
    put(client, field("name", "Asha"), field("email", "a@x.com"))
    assert client.delete("/api/profile/fields/name").status_code == 204
    profile = client.get("/api/profile").json()
    assert profile["summary"]["name"] is None
    assert positions(profile) == {"email": 2}
    assert client.delete("/api/profile/fields/name").status_code == 404


def test_resume(client):
    response = client.get("/api/profile/resume")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("inline")
    assert 'filename="Test_Resume.pdf"' in disposition
    assert response.content == FAKE_PDF


def test_missing_resume(client, resume_path):
    resume_path.unlink()
    assert client.get("/api/profile/resume").status_code == 404
    assert client.get("/api/profile").json()["summary"]["resume"] is None
