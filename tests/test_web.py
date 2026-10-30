"""Phase 3 dashboard: overview, epic detail, feedback, event view and report drafts."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from pewa.connectors.fixtures import FixtureConnector
from pewa.db import make_engine, session_scope
from pewa.ingest import ingest
from pewa.models import Assessment, Feedback, Run
from pewa.schemas import SOURCES
from pewa.web.app import create_app, evidence_href


@pytest.fixture
def engine(tmp_path) -> Engine:
    engine = make_engine(tmp_path / "web.db")
    with session_scope(engine) as s:
        ingest(s, "atlas", [FixtureConnector("atlas", src) for src in SOURCES])
    return engine


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(engine, project="atlas", dry_run=True)) as c:
        yield c


def run_at(client: TestClient, as_of: str) -> int:
    response = client.post("/runs", data={"as_of": as_of}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return int(response.headers["location"].split("run=")[1])


def assessment_id(engine: Engine, run_id: int, epic: str) -> int:
    with session_scope(engine) as s:
        return s.scalar(select(Assessment.id).where(Assessment.run_id == run_id, Assessment.epic_key == epic))


def test_evidence_links_open_real_sources_but_not_fixture_urls():
    assert evidence_href("evt_1", "https://acme.atlassian.net/browse/X-1") == "https://acme.atlassian.net/browse/X-1"
    assert evidence_href("evt_1", "https://github.example/a/b/pull/1") == "/events/evt_1"
    assert evidence_href("evt_1", None) == "/events/evt_1"


def test_empty_overview_explains_next_step(client: TestClient):
    page = client.get("/").text
    assert "No assessments yet" in page and "pewa ingest --fixtures atlas" in page


def test_run_from_dashboard_then_overview(client: TestClient):
    run_id = run_at(client, "2026-02-13")
    page = client.get(f"/?run={run_id}").text
    for key in ("ATL-1", "ATL-2", "ATL-3", "ATL-4", "ATL-5"):
        assert key in page
    assert 'class="badge b-red"' in page and 'class="badge b-green"' in page
    assert "dry-run" in page
    assert client.get("/").text.count("ATL-1") == page.count("ATL-1")  # latest run is the default


def test_trend_compares_with_the_previous_run(client: TestClient, engine: Engine):
    run_at(client, "2026-01-23")
    later = run_at(client, "2026-02-13")
    page = client.get(f"/?run={later}").text
    assert "trend vs. 23 Jan" in page
    assert "↑ worse" in page  # ATL-1 was green in week 3 and is red by week 6


def test_date_without_a_run_shows_notice(client: TestClient):
    run_at(client, "2026-02-06")
    page = client.get("/?as_of=2026-02-13").text
    assert "No assessment as of Fri 13 Feb 2026; showing the latest earlier run (06 Feb)" in page
    assert "Run assessment for 2026-02-13" in page


def test_bad_inputs(client: TestClient):
    assert client.get("/?as_of=not-a-date").status_code == 400
    assert client.get("/?run=999").status_code == 404
    assert client.get("/assessments/999").status_code == 404
    assert client.get("/events/evt_nope").status_code == 404


def test_epic_detail_shows_risks_timeline_and_history(client: TestClient, engine: Engine):
    run_at(client, "2026-02-06")
    run_id = run_at(client, "2026-02-13")
    page = client.get(f"/assessments/{assessment_id(engine, run_id, 'ATL-1')}").text
    assert "ATL-1 Payments API" in page
    assert "Key risks" in page and "Suggested actions" in page
    assert "Signal timeline" in page and "blocker_language" in page and ">cited<" in page
    assert 'href="/events/evt_' in page  # fixture URLs open the local event view
    assert page.count('class="badge b-red"') >= 3  # heading plus two runs of history


def test_feedback_persists_via_htmx_and_plain_form(client: TestClient, engine: Engine):
    aid = assessment_id(engine, run_at(client, "2026-02-13"), "ATL-1")

    partial = client.post(f"/assessments/{aid}/feedback", data={"verdict": "useful", "note": " caught it early "},
                          headers={"HX-Request": "true"})
    assert partial.status_code == 200
    assert "Saved." in partial.text and "caught it early" in partial.text and "<html" not in partial.text

    redirect = client.post(f"/assessments/{aid}/feedback", data={"verdict": "noise"}, follow_redirects=False)
    assert redirect.status_code == 303 and redirect.headers["location"] == f"/assessments/{aid}#feedback"

    assert client.post(f"/assessments/{aid}/feedback", data={"verdict": "meh"}).status_code == 400

    with session_scope(engine) as s:
        rows = s.scalars(select(Feedback).where(Feedback.assessment_id == aid).order_by(Feedback.id)).all()
        assert [(f.verdict, f.note) for f in rows] == [("useful", "caught it early"), ("noise", None)]
    assert "👍 1 · 👎 1" in client.get("/").text


def test_event_view(client: TestClient, engine: Engine):
    run_id = run_at(client, "2026-02-13")
    with session_scope(engine) as s:
        a = s.scalar(select(Assessment).where(Assessment.run_id == run_id, Assessment.epic_key == "ATL-1"))
        event_id = a.evidence[0]["id"]
    page = client.get(f"/events/{event_id}").text
    assert event_id in page and "fictional fixture URL" in page and "Payload" in page


def test_reports_page_and_drafts(client: TestClient):
    assert "No assessment runs yet" in client.get("/reports").text
    run_id = run_at(client, "2026-02-13")
    assert "Generate exec summary" in client.get("/reports").text

    partial = client.post("/reports", data={"run_id": run_id, "variant": "exec"}, headers={"HX-Request": "true"})
    assert partial.status_code == 200 and "<html" not in partial.text
    assert "**Overall: RED**" in partial.text and "Copy Markdown" in partial.text

    full = client.post("/reports", data={"run_id": run_id, "variant": "eng"})
    assert "<html" in full.text and "engineering update" in full.text

    assert client.post("/reports", data={"run_id": run_id, "variant": "poem"}).status_code == 400
    assert client.post("/reports", data={"run_id": 999, "variant": "exec"}).status_code == 404


def test_runs_store_owner_and_due_date(client: TestClient, engine: Engine):
    run_id = run_at(client, "2026-02-13")
    with session_scope(engine) as s:
        a = s.scalar(select(Assessment).where(Assessment.run_id == run_id, Assessment.epic_key == "ATL-1"))
        assert a.detail["owner"] == "diego.ferreira" and a.detail["due_date"] == "2026-02-20"
        assert s.get(Run, run_id).label == "dashboard"
