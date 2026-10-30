"""Run connectors and append their events to the store."""

import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from pewa.connectors.base import Connector, ConnectorError
from pewa.entities import build_versions, entity_type
from pewa.linking import LinkResolver
from pewa.models import EntityVersion, EventRow, Outcome
from pewa.schemas import Event, make_event_id, status_category
from pewa.snapshot import build as build_snapshot
from pewa.snapshot import load_entities, row_to_event

log = logging.getLogger(__name__)

_CHUNK = 500


@dataclass
class IngestResult:
    fetched: dict[str, int] = field(default_factory=dict)
    inserted: dict[str, int] = field(default_factory=dict)
    entity_versions: int = 0
    linked_events: int = 0
    outcomes: int = 0
    errors: dict[str, str] = field(default_factory=dict)  # source -> why it was skipped


def _chunks(items: Sequence[Any], size: int = _CHUNK) -> Iterable[Sequence[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def last_ingested_at(session: Session, project: str, source: str) -> datetime | None:
    stmt = select(func.max(EventRow.occurred_at)).where(EventRow.project == project, EventRow.source == source)
    return session.scalar(stmt)  # max() keeps the column type, so this is an aware UTC datetime


def insert_events(session: Session, project: str, events: Iterable[Event]) -> list[Event]:
    """Insert events not already stored. Returns the newly inserted events (with ids)."""
    pending: dict[str, Event] = {}
    for event in events:
        event_id = make_event_id(project, event.source, event.kind, event.entity_id, event.occurred_at)
        pending.setdefault(event_id, event.model_copy(update={"id": event_id}))

    ids = list(pending)
    existing: set[str] = set()
    for chunk in _chunks(ids):
        existing.update(session.scalars(select(EventRow.id).where(EventRow.id.in_(chunk))))

    new = [e for i, e in pending.items() if i not in existing]
    session.add_all(
        EventRow(
            id=e.id,
            project=project,
            source=e.source,
            kind=e.kind,
            occurred_at=e.occurred_at,
            entity_id=e.entity_id,
            epic_key=None,
            actor=e.actor,
            payload=e.payload,
            url=e.url,
        )
        for e in new
    )
    session.flush()
    return new


def rebuild_entity_versions(session: Session, project: str, touched: Iterable[Event]) -> int:
    """Recompute versions for every entity touched by `touched` from its full event history."""
    keys_by_type: dict[str, set[str]] = defaultdict(set)
    for event in touched:
        if etype := entity_type(event):
            keys_by_type[etype].add(event.entity_id)

    written = 0
    for etype, keys in keys_by_type.items():
        source = "jira" if etype == "ticket" else "github"
        history: dict[str, list[Event]] = defaultdict(list)
        for chunk in _chunks(sorted(keys)):
            session.execute(
                delete(EntityVersion).where(
                    EntityVersion.project == project,
                    EntityVersion.entity_type == etype,
                    EntityVersion.entity_key.in_(chunk),
                )
            )
            rows = session.scalars(
                select(EventRow).where(
                    EventRow.project == project, EventRow.source == source, EventRow.entity_id.in_(chunk)
                )
            )
            for row in rows:
                history[row.entity_id].append(row_to_event(row))
        for key, events in history.items():
            for valid_from, data in build_versions(events):
                session.add(
                    EntityVersion(project=project, entity_type=etype, entity_key=key, valid_from=valid_from, data=data)
                )
                written += 1
    session.flush()
    return written


def relink_events(session: Session, project: str) -> int:
    """Refresh the stored epic_key of every event using the latest known links.

    Snapshots re-resolve links as of their own date; this column is a convenience index.
    """
    tickets, prs = load_entities(session, project, as_of=None)
    resolver = LinkResolver(tickets, prs)
    rows = list(
        session.scalars(select(EventRow).where(EventRow.project == project).order_by(EventRow.occurred_at, EventRow.id))
    )
    links = resolver.resolve(row_to_event(r) for r in rows)
    changed = 0
    for row in rows:
        if row.epic_key != links[row.id]:
            row.epic_key = links[row.id]
            changed += 1
    session.flush()
    return changed


def derive_outcomes(session: Session, project: str) -> list[dict[str, Any]]:
    """Ground truth from finished epics: planned = first due date set, actual = when it reached Done."""
    state = build_snapshot(session, project, datetime.now(UTC))
    rows = []
    for epic in state.epics.values():
        if not (epic.original_due_date and epic.resolved_at and status_category(epic.status) == "done"):
            continue
        planned, actual = date.fromisoformat(epic.original_due_date), epic.resolved_at.date()
        rows.append({"epic_key": epic.key, "planned_date": planned, "actual_date": actual, "slipped": actual > planned})
    return rows


def save_outcomes(session: Session, project: str, outcomes: Iterable[dict[str, Any]]) -> int:
    count = 0
    for outcome in outcomes:
        row = session.scalar(
            select(Outcome).where(Outcome.project == project, Outcome.epic_key == outcome["epic_key"])
        ) or Outcome(project=project, epic_key=outcome["epic_key"])
        row.planned_date = outcome["planned_date"]
        row.actual_date = outcome["actual_date"]
        row.slipped = outcome["slipped"]
        session.add(row)
        count += 1
    session.flush()
    return count


def ingest(session: Session, project: str, connectors: Iterable[Connector], incremental: bool = True) -> IngestResult:
    result = IngestResult()
    new_events: list[Event] = []
    for connector in connectors:
        since = last_ingested_at(session, project, connector.source) if incremental else None
        try:
            events = connector.fetch(since)
        except ConnectorError as exc:
            # One unreachable source shouldn't block the others; the caller reports it.
            result.errors[connector.source] = str(exc)
            log.warning("ingest %s/%s skipped: %s", project, connector.source, exc)
            continue
        inserted = insert_events(session, project, events)
        result.fetched[connector.source] = len(events)
        result.inserted[connector.source] = len(inserted)
        new_events += inserted
        log.info("ingest %s/%s: fetched=%d inserted=%d", project, connector.source, len(events), len(inserted))

    if new_events:
        result.entity_versions = rebuild_entity_versions(session, project, new_events)
        result.linked_events = relink_events(session, project)
    return result
