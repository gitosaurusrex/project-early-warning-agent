from sqlalchemy import func, select
from sqlalchemy.orm import Session

from conftest import ListConnector, ev, utc
from pewa.connectors.fixtures import FixtureConnector, load_outcomes
from pewa.ingest import ingest, last_ingested_at, save_outcomes
from pewa.models import EntityVersion, EventRow, Outcome
from pewa.schemas import SOURCES


def count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_atlas_ingest_loads_everything(session: Session):
    result = ingest(session, "atlas", [FixtureConnector("atlas", s) for s in SOURCES])
    expected = {s: len(FixtureConnector("atlas", s).fetch()) for s in SOURCES}
    assert result.inserted == expected
    assert count(session, EventRow) == sum(expected.values())
    assert count(session, EntityVersion) > 0
    # Most events link to an epic; standup roots and channel chatter stay unlinked.
    linked = session.scalar(select(func.count()).where(EventRow.epic_key.is_not(None)))
    assert 0.7 * sum(expected.values()) < linked < sum(expected.values())


def test_reingest_is_idempotent(session: Session):
    connectors = [FixtureConnector("atlas", s) for s in SOURCES]
    ingest(session, "atlas", connectors)
    before = count(session, EventRow), count(session, EntityVersion)
    again = ingest(session, "atlas", connectors, incremental=False)
    assert sum(again.inserted.values()) == 0
    assert (count(session, EventRow), count(session, EntityVersion)) == before


def test_incremental_sync_fetches_from_last_event(session: Session):
    first = [ev("jira", "ticket_created", "ATL-1", utc(2026, 1, 1), issue_type="Epic", title="E", status="To Do")]
    ingest(session, "p", [ListConnector("jira", first)])
    assert last_ingested_at(session, "p", "jira") == utc(2026, 1, 1)

    calls = []

    class Recording(ListConnector):
        def fetch(self, since):
            calls.append(since)
            return super().fetch(since)

    later = first + [ev("jira", "status_changed", "ATL-1", utc(2026, 1, 2), **{"from": "To Do", "to": "Done"})]
    result = ingest(session, "p", [Recording("jira", later)])
    assert calls == [utc(2026, 1, 1)]
    assert result.fetched["jira"] == 2 and result.inserted["jira"] == 1  # boundary event re-fetched, not duplicated


def test_projects_are_isolated(session: Session):
    events = [ev("jira", "ticket_created", "ATL-1", utc(2026, 1, 1), issue_type="Epic", title="E", status="To Do")]
    ingest(session, "one", [ListConnector("jira", events)])
    ingest(session, "two", [ListConnector("jira", events)])
    assert count(session, EventRow) == 2


def test_outcomes_upsert(session: Session):
    outcomes = load_outcomes("atlas")
    assert save_outcomes(session, "atlas", outcomes) == 5
    save_outcomes(session, "atlas", outcomes)
    assert count(session, Outcome) == 5
    slipped = set(session.scalars(select(Outcome.epic_key).where(Outcome.slipped)))
    assert slipped == {"ATL-1", "ATL-2"}
