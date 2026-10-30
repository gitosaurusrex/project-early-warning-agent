"""The Atlas dataset matches SPEC §11 and the generator reproduces the committed files."""

import importlib.util
import json
import sys
from datetime import date

import pytest
from sqlalchemy.orm import Session

from pewa import snapshot
from pewa.config import REPO_ROOT
from pewa.connectors.fixtures import load_meta, load_outcomes
from pewa.schemas import WORKFLOW

ATLAS = REPO_ROOT / "data" / "fixtures" / "atlas"


def business_days_between(a: date, b: date) -> int:
    return sum(1 for n in range(1, (b - a).days + 1) if date.fromordinal(a.toordinal() + n).weekday() < 5)


@pytest.fixture(scope="module")
def end_state(atlas_engine):
    from pewa.db import session_scope

    with session_scope(atlas_engine) as s:
        return snapshot.build(s, "atlas", snapshot.parse_as_of("2026-03-13"))


def test_generator_is_deterministic_and_matches_committed_files(tmp_path):
    spec = importlib.util.spec_from_file_location("make_fixtures", REPO_ROOT / "scripts" / "make_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    module.write(tmp_path)
    for name in ("jira.json", "github.json", "slack.json", "outcomes.json", "meta.json"):
        assert (tmp_path / name).read_text() == (ATLAS / name).read_text(), f"{name} is stale: rerun make_fixtures.py"


def test_dataset_size(end_state):
    meta = load_meta("atlas")
    engineers = [p for p in meta["people"].values() if p["role"] == "engineer"]
    messages = [e for e in end_state.events if e.source == "slack"]
    assert len(engineers) == 6
    assert len(end_state.epics) == 5
    assert 75 <= len(end_state.tickets) + len(end_state.epics) <= 85
    assert 55 <= len(end_state.prs) <= 75
    assert 380 <= len(messages) <= 440
    first, last = min(e.occurred_at for e in end_state.events), max(e.occurred_at for e in end_state.events)
    assert (last - first).days <= 10 * 7


def test_outcomes():
    outcomes = {o["epic_key"]: o for o in load_outcomes("atlas")}
    assert outcomes["ATL-1"]["slipped"] and (outcomes["ATL-1"]["actual_date"] - outcomes["ATL-1"]["planned_date"]).days == 14
    assert outcomes["ATL-2"]["slipped"] and (outcomes["ATL-2"]["actual_date"] - outcomes["ATL-2"]["planned_date"]).days == 7
    assert not any(outcomes[k]["slipped"] for k in ("ATL-3", "ATL-4", "ATL-5"))


def test_outcomes_agree_with_jira_history(end_state):
    for o in load_outcomes("atlas"):
        epic = end_state.epics[o["epic_key"]]
        assert epic.original_due_date == o["planned_date"].isoformat()
        assert epic.resolved_at.date() == o["actual_date"]


def test_epic_a_pr_waits_eight_business_days_on_another_team(end_state):
    epic = end_state.epics["ATL-1"]
    ledger = next(pr for pr in end_state.epic_prs("ATL-1") if "ledger" in pr.title.lower())
    requested, first_review = ledger.review_requests[0].at, ledger.reviews[0].at
    assert business_days_between(requested.date(), first_review.date()) >= 8
    assert ledger.review_requests[0].reviewer == "ken.ibarra"  # ledger team, not Atlas
    # The evidence is visible well over a week before the original due date.
    assert (date.fromisoformat(epic.original_due_date) - requested.date()).days >= 14
    blockers = [e for e in end_state.epic_events("ATL-1") if e.source == "slack" and "waiting on" in e.payload["text"].lower()]
    assert blockers and min(e.occurred_at.date() for e in blockers) < date(2026, 2, 13)


def test_epic_b_scope_grows_forty_percent_mid_flight(atlas: Session):
    started = snapshot.build(atlas, "atlas", snapshot.parse_as_of("2026-01-30"))
    later = snapshot.build(atlas, "atlas", snapshot.parse_as_of("2026-02-13"))
    before, after = len(started.epics["ATL-2"].children), len(later.epics["ATL-2"].children)
    assert started.epics["ATL-2"].status == "In Progress"
    assert after / before >= 1.4


def test_epic_c_is_noisy_but_on_time(end_state):
    events = end_state.epic_events("ATL-3")
    backwards = [
        e for e in events
        if e.kind == "status_changed" and WORKFLOW.get(e.payload["to"], 0) < WORKFLOW.get(e.payload["from"], 0)
    ]
    assert backwards, "expected a reopened ticket"
    assert any(pr.ci_failures >= 2 for pr in end_state.epic_prs("ATL-3"))
    chatter = {k: sum(1 for e in end_state.epic_events(k) if e.source == "slack") for k in end_state.epics}
    assert chatter["ATL-3"] == max(chatter.values())


def test_fixture_files_are_well_formed():
    for name in ("jira.json", "github.json", "slack.json"):
        records = json.loads((ATLAS / name).read_text())
        assert records == sorted(records, key=lambda r: (r["occurred_at"], r["kind"], r["entity_id"]))
        assert all({"kind", "occurred_at", "entity_id", "actor", "payload", "url"} <= r.keys() for r in records)
