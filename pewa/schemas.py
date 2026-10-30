"""Pydantic models shared across the app: events, entity state and project snapshots."""

import hashlib
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Source = Literal["jira", "github", "slack"]
SOURCES: tuple[Source, ...] = ("jira", "github", "slack")

# Jira workflow, in order. Used to map statuses to categories and to detect backwards moves.
WORKFLOW: dict[str, int] = {"To Do": 0, "In Progress": 1, "In Review": 2, "Done": 3}
DONE_STATUSES = {"Done"}
TODO_STATUSES = {"To Do"}


def status_category(status: str | None) -> Literal["todo", "in_progress", "done"]:
    if status in DONE_STATUSES:
        return "done"
    if status in TODO_STATUSES or status is None:
        return "todo"
    return "in_progress"


def to_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime; naive values are assumed to already be UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def make_event_id(project: str, source: str, kind: str, entity_id: str, occurred_at: datetime) -> str:
    raw = f"{project}|{source}|{kind}|{entity_id}|{to_utc(occurred_at).isoformat()}"
    return "evt_" + hashlib.sha1(raw.encode()).hexdigest()[:12]


class Event(BaseModel):
    """One immutable thing that happened in a source system."""

    id: str | None = None  # assigned at ingest, stable across re-ingests
    source: Source
    kind: str
    occurred_at: datetime
    entity_id: str
    epic_key: str | None = None
    actor: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    url: str | None = None

    @field_validator("occurred_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return to_utc(value)


class TicketState(BaseModel):
    key: str
    issue_type: str
    title: str
    status: str
    assignee: str | None = None
    parent: str | None = None
    points: float | None = None
    due_date: str | None = None
    original_due_date: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None
    url: str | None = None
    last_activity_at: datetime | None = None
    last_activity_event_id: str | None = None

    @property
    def category(self) -> str:
        return status_category(self.status)

    @property
    def is_epic(self) -> bool:
        return self.issue_type == "Epic"


class ReviewRequest(BaseModel):
    reviewer: str
    at: datetime


class Review(BaseModel):
    reviewer: str
    state: str
    at: datetime


class PRState(BaseModel):
    key: str  # "owner/repo#number"
    repo: str
    number: int
    title: str
    branch: str | None = None
    author: str | None = None
    state: Literal["open", "merged", "closed"] = "open"
    refs: list[str] = Field(default_factory=list)
    epic_key: str | None = None
    opened_at: datetime
    merged_at: datetime | None = None
    closed_at: datetime | None = None
    url: str | None = None
    review_requests: list[ReviewRequest] = Field(default_factory=list)
    reviews: list[Review] = Field(default_factory=list)
    ci_failures: int = 0


class EpicState(BaseModel):
    key: str
    title: str
    status: str
    owner: str | None = None
    due_date: str | None = None
    original_due_date: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None
    url: str | None = None
    children: list[str] = Field(default_factory=list)


class Signal(BaseModel):
    """Output of a deterministic detector (SPEC §9). Evidence lists event ids."""

    type: str
    severity: int = Field(ge=1, le=3)
    epic_key: str
    evidence: list[str] = Field(default_factory=list)
    detail: str


class ProjectState(BaseModel):
    """Everything known about a project as of a moment in time, and nothing after it."""

    project: str
    as_of: datetime
    epics: dict[str, EpicState] = Field(default_factory=dict)
    tickets: dict[str, TicketState] = Field(default_factory=dict)
    prs: dict[str, PRState] = Field(default_factory=dict)
    events: list[Event] = Field(default_factory=list)

    def epic_events(self, epic_key: str) -> list[Event]:
        return [e for e in self.events if e.epic_key == epic_key]

    def epic_children(self, epic_key: str) -> list[TicketState]:
        return [self.tickets[k] for k in self.epics[epic_key].children]

    def epic_prs(self, epic_key: str) -> list[PRState]:
        return [pr for pr in self.prs.values() if pr.epic_key == epic_key]
