"""SPEC §8: a snapshot as of T must contain nothing that happened after T."""

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from conftest import ListConnector, ev, ingest_events, utc
from pewa import snapshot
from pewa.connectors.fixtures import FixtureConnector
from pewa.db import make_engine, session_scope
from pewa.ingest import ingest
from pewa.schemas import SOURCES

P = "t"
URL = "https://github.example/acme/api/pull/7"


def history() -> list:
    return [
        ev("jira", "ticket_created", "ATL-1", utc(2026, 1, 1, 9), issue_type="Epic", title="Payments", status="To Do",
           assignee="dee", due_date="2026-02-01"),
        ev("jira", "ticket_created", "ATL-9", utc(2026, 1, 1, 9, 1), issue_type="Epic", title="Search", status="To Do"),
        ev("jira", "ticket_created", "ATL-2", utc(2026, 1, 1, 10), issue_type="Story", title="Charge", status="To Do",
           assignee="dee", parent="ATL-1", points=3),
        ev("jira", "ticket_created", "ATL-4", utc(2026, 1, 1, 10, 5), issue_type="Story", title="Movable",
           status="To Do", parent="ATL-1"),
        ev("jira", "status_changed", "ATL-2", utc(2026, 1, 2, 10), "dee", **{"from": "To Do", "to": "In Progress"}),
        ev("github", "pr_opened", "acme/api#7", utc(2026, 1, 3, 14), "dee", repo="acme/api", number=7,
           title="ATL-2: charge endpoint", branch="dee/charge", body=""),
        ev("github", "pr_review_requested", "acme/api#7", utc(2026, 1, 3, 14, 1), "dee", reviewer="sam"),
        ev("github", "ci_failed", "acme/api#7", utc(2026, 1, 4, 9), "ci", check="test"),
        ev("slack", "message", "C1:100.1", utc(2026, 1, 4, 11), "sam", channel="C1", ts="100.1", text=f"Looking at {URL}"),
        # ---- everything below happens after 2026-01-05 ----
        ev("slack", "message", "C1:200.1", utc(2026, 1, 6, 11), "dee", channel="C1", ts="200.1", thread_ts="100.1",
           text="blocked on infra"),
        ev("jira", "ticket_created", "ATL-3", utc(2026, 1, 7, 9), issue_type="Story", title="Late scope",
           status="To Do", parent="ATL-1"),
        ev("github", "ci_failed", "acme/api#7", utc(2026, 1, 7, 9), "ci", check="test"),
        ev("jira", "due_date_changed", "ATL-1", utc(2026, 1, 8, 9), "rosa", **{"from": "2026-02-01", "to": "2026-02-15"}),
        ev("jira", "parent_changed", "ATL-4", utc(2026, 1, 8, 10), "rosa", **{"from": "ATL-1", "to": "ATL-9"}),
        ev("github", "pr_reviewed", "acme/api#7", utc(2026, 1, 9, 10), "sam", state="approved"),
        ev("github", "pr_merged", "acme/api#7", utc(2026, 1, 9, 11), "dee"),
        ev("jira", "status_changed", "ATL-2", utc(2026, 1, 10, 10), "dee", **{"from": "In Progress", "to": "Done"}),
    ]


AS_OF = utc(2026, 1, 5, 23, 59)


@pytest.fixture
def state(session: Session):
    ingest_events(session, P, history())
    return snapshot.build(session, P, AS_OF)


def test_only_events_up_to_as_of(state):
    assert state.events
    assert all(e.occurred_at <= AS_OF for e in state.events)
    assert "C1:200.1" not in {e.entity_id for e in state.events}


def test_ticket_closed_later_appears_open(state):
    ticket = state.tickets["ATL-2"]
    assert ticket.status == "In Progress"
    assert ticket.resolved_at is None


def test_ticket_created_later_is_absent(state):
    assert "ATL-3" not in state.tickets
    assert "ATL-3" not in state.epics["ATL-1"].children


def test_due_date_change_later_is_invisible(state):
    epic = state.epics["ATL-1"]
    assert epic.due_date == "2026-02-01"
    assert epic.original_due_date == "2026-02-01"


def test_reparenting_later_does_not_move_ticket_or_its_events(state):
    assert state.epics["ATL-1"].children == ["ATL-2", "ATL-4"]
    assert state.epics["ATL-9"].children == []
    created = next(e for e in state.events if e.entity_id == "ATL-4")
    assert created.epic_key == "ATL-1"


def test_pr_merged_later_appears_open_and_unreviewed(state):
    pr = state.prs["acme/api#7"]
    assert pr.state == "open"
    assert pr.merged_at is None
    assert pr.reviews == []
    assert [r.reviewer for r in pr.review_requests] == ["sam"]
    assert pr.ci_failures == 1  # the second failure happens later


def test_last_activity_ignores_future(state):
    assert state.tickets["ATL-2"].last_activity_at == utc(2026, 1, 4, 11)  # the Slack PR link


def test_later_snapshot_sees_the_future(session: Session):
    ingest_events(session, P, history())
    later = snapshot.build(session, P, utc(2026, 1, 10, 12))
    assert later.tickets["ATL-2"].status == "Done"
    assert later.epics["ATL-1"].due_date == "2026-02-15"
    assert later.epics["ATL-1"].original_due_date == "2026-02-01"
    assert later.epics["ATL-1"].children == ["ATL-2", "ATL-3"]
    assert later.epics["ATL-9"].children == ["ATL-4"]
    assert later.prs["acme/api#7"].state == "merged"
    assert later.prs["acme/api#7"].ci_failures == 2


def test_event_exactly_at_as_of_is_included(session: Session):
    ingest_events(session, P, history())
    state = snapshot.build(session, P, utc(2026, 1, 9, 11))
    assert state.prs["acme/api#7"].state == "merged"
    state = snapshot.build(session, P, utc(2026, 1, 9, 11) - timedelta(microseconds=1))
    assert state.prs["acme/api#7"].state == "open"


def test_before_anything_happened_is_empty(session: Session):
    ingest_events(session, P, history())
    state = snapshot.build(session, P, utc(2025, 12, 31))
    assert state.events == [] and state.tickets == {} and state.epics == {} and state.prs == {}


def test_out_of_order_ingestion_gives_same_snapshot(session: Session, tmp_path):
    """Future events ingested first, history back-filled later: snapshots must not change."""
    events = history()
    late, early = [e for e in events if e.occurred_at > AS_OF], [e for e in events if e.occurred_at <= AS_OF]
    ingest_events(session, P, late)
    ingest_events(session, P, early)
    reordered = snapshot.build(session, P, AS_OF)

    with session_scope(make_engine(tmp_path / "clean.db")) as clean:
        ingest_events(clean, P, events)
        assert reordered == snapshot.build(clean, P, AS_OF)


@pytest.mark.parametrize("as_of", ["2026-01-09", "2026-01-30", "2026-02-13", "2026-02-20", "2026-03-06"])
def test_atlas_snapshot_matches_store_truncated_at_as_of(atlas: Session, tmp_path, as_of):
    """The strongest check: a store holding the full history must give exactly the same snapshot
    as a store that only ever saw events up to as_of. Any leak (entity versions, links,
    derived PR or ticket fields) would make them differ."""
    when = snapshot.parse_as_of(as_of)
    full = snapshot.build(atlas, "atlas", when)

    with session_scope(make_engine(tmp_path / "truncated.db")) as truncated:
        past = [e for s in SOURCES for e in FixtureConnector("atlas", s).fetch() if e.occurred_at <= when]
        ingest(truncated, "atlas", [ListConnector(s, past) for s in SOURCES], incremental=False)
        expected = snapshot.build(truncated, "atlas", when)

    assert full.events and full == expected
    assert all(e.occurred_at <= when for e in full.events)
    assert all(t.created_at <= when for t in full.tickets.values())
    assert all(pr.opened_at <= when for pr in full.prs.values())
