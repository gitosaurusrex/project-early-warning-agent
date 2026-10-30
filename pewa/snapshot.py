"""Build a ProjectState as of a timestamp, using only data that existed at that moment (SPEC §8).

Two rules keep snapshots honest:
- only events with occurred_at <= as_of are read, and
- entity state comes from the latest version with valid_from <= as_of; links between events,
  tickets and epics are resolved from that state, not from whatever is known today.
"""

import re
from datetime import UTC, date, datetime, time
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from pewa.linking import LinkResolver
from pewa.models import EntityVersion, EventRow
from pewa.schemas import EpicState, Event, PRState, ProjectState, Review, ReviewRequest, TicketState, to_utc

EntityMap = dict[str, dict[str, Any]]


def parse_as_of(value: str | date | datetime) -> datetime:
    """A bare date means the end of that day (UTC); datetimes without a zone are taken as UTC."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value) if ("T" in value or " " in value) else date.fromisoformat(value)
    if isinstance(value, datetime):
        return to_utc(value)
    return datetime.combine(value, time.max, tzinfo=UTC)


def natural_key(key: str) -> tuple[str, int]:
    match = re.match(r"^(.*?)(\d+)$", key)
    return (match.group(1), int(match.group(2))) if match else (key, 0)


def row_to_event(row: EventRow) -> Event:
    return Event(
        id=row.id,
        source=row.source,  # type: ignore[arg-type]
        kind=row.kind,
        occurred_at=row.occurred_at,
        entity_id=row.entity_id,
        epic_key=row.epic_key,
        actor=row.actor,
        payload=row.payload,
        url=row.url,
    )


def load_entities(session: Session, project: str, as_of: datetime | None) -> tuple[EntityMap, EntityMap]:
    """Latest version of every ticket and PR with valid_from <= as_of (or the latest overall)."""
    conditions = [EntityVersion.project == project]
    if as_of is not None:
        conditions.append(EntityVersion.valid_from <= as_of)
    latest = (
        select(
            EntityVersion.entity_type,
            EntityVersion.entity_key,
            func.max(EntityVersion.valid_from).label("valid_from"),
        )
        .where(*conditions)
        .group_by(EntityVersion.entity_type, EntityVersion.entity_key)
        .subquery()
    )
    rows = session.scalars(
        select(EntityVersion).join(
            latest,
            and_(
                EntityVersion.project == project,
                EntityVersion.entity_type == latest.c.entity_type,
                EntityVersion.entity_key == latest.c.entity_key,
                EntityVersion.valid_from == latest.c.valid_from,
            ),
        )
    )
    tickets: EntityMap = {}
    prs: EntityMap = {}
    for row in rows:
        (tickets if row.entity_type == "ticket" else prs)[row.entity_key] = row.data
    return tickets, prs


def build(session: Session, project: str, as_of: datetime) -> ProjectState:
    as_of = to_utc(as_of)
    ticket_data, pr_data = load_entities(session, project, as_of)
    resolver = LinkResolver(ticket_data, pr_data)

    rows = session.scalars(
        select(EventRow)
        .where(EventRow.project == project, EventRow.occurred_at <= as_of)
        .order_by(EventRow.occurred_at, EventRow.id)
    )
    raw_events = [row_to_event(r) for r in rows]
    links = resolver.resolve(raw_events)
    events = [e.model_copy(update={"epic_key": links[e.id]}) for e in raw_events]

    prs = {key: PRState(**data, epic_key=resolver.epic_for_pr(key)) for key, data in pr_data.items()}
    last_activity: dict[str, datetime] = {}
    for event in events:
        for ref in resolver.ticket_refs(event):
            last_activity[ref] = event.occurred_at
        pr = prs.get(event.entity_id) if event.source == "github" else None
        if pr is None:
            continue
        if event.kind == "pr_review_requested":
            pr.review_requests.append(ReviewRequest(reviewer=event.payload.get("reviewer", ""), at=event.occurred_at))
        elif event.kind == "pr_reviewed":
            pr.reviews.append(
                Review(reviewer=event.actor or "", state=event.payload.get("state", ""), at=event.occurred_at)
            )
        elif event.kind == "ci_failed":
            pr.ci_failures += 1

    epics: dict[str, EpicState] = {}
    tickets: dict[str, TicketState] = {}
    for key, data in ticket_data.items():
        if data.get("issue_type") == "Epic":
            epics[key] = EpicState(
                key=key,
                title=data["title"],
                status=data["status"],
                owner=data.get("assignee"),
                due_date=data.get("due_date"),
                original_due_date=data.get("original_due_date"),
                created_at=data["created_at"],
                resolved_at=data.get("resolved_at"),
                url=data.get("url"),
            )
        else:
            tickets[key] = TicketState(**data, last_activity_at=last_activity.get(key))

    for key in sorted(tickets, key=natural_key):
        epic = resolver.epic_for_ticket(key)
        if epic in epics:
            epics[epic].children.append(key)

    return ProjectState(
        project=project,
        as_of=as_of,
        epics=dict(sorted(epics.items(), key=lambda kv: natural_key(kv[0]))),
        tickets=dict(sorted(tickets.items(), key=lambda kv: natural_key(kv[0]))),
        prs=prs,
        events=events,
    )
