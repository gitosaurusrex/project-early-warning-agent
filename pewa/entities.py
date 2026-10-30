"""Fold events into versioned entity state (tickets, epics, PRs).

Entity versions are a derived view of the append-only events table: each version is the
entity's full state from `valid_from` until the next version.
"""

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from pewa.linking import pr_refs
from pewa.schemas import Event, status_category

# Within one timestamp, creation must be applied before changes.
_KIND_ORDER = {"ticket_created": 0, "pr_opened": 0}


def entity_type(event: Event) -> str | None:
    if event.source == "jira":
        return "ticket"
    if event.source == "github":
        return "pr"
    return None


def sort_key(event: Event) -> tuple[datetime, int, str]:
    return (event.occurred_at, _KIND_ORDER.get(event.kind, 1), event.id or "")


def apply_event(data: dict[str, Any] | None, event: Event) -> dict[str, Any] | None:
    """Return the entity state after `event`. Returns `data` itself when nothing changed."""
    p = event.payload
    when = event.occurred_at.isoformat()

    if event.kind == "ticket_created":
        return {
            "key": event.entity_id,
            "issue_type": p.get("issue_type", "Task"),
            "title": p.get("title", ""),
            "status": p.get("status", "To Do"),
            "assignee": p.get("assignee"),
            "parent": p.get("parent"),
            "points": p.get("points"),
            "due_date": p.get("due_date"),
            "original_due_date": p.get("due_date"),
            "created_at": when,
            "resolved_at": when if status_category(p.get("status")) == "done" else None,
            "url": event.url,
        }
    if event.kind == "pr_opened":
        return {
            "key": event.entity_id,
            "repo": p.get("repo"),
            "number": p.get("number"),
            "title": p.get("title", ""),
            "branch": p.get("branch"),
            "author": event.actor,
            "state": "open",
            "refs": pr_refs(p.get("title"), p.get("branch"), p.get("body")),
            "opened_at": when,
            "merged_at": None,
            "closed_at": None,
            "url": event.url,
        }
    if data is None:
        return None  # change to an entity we never saw created

    new = dict(data)
    match event.kind:
        case "status_changed":
            new["status"] = p.get("to")
            done = status_category(new["status"]) == "done"
            new["resolved_at"] = (data.get("resolved_at") or when) if done else None
        case "assignee_changed":
            new["assignee"] = p.get("to")
        case "due_date_changed":
            new["due_date"] = p.get("to")
            new["original_due_date"] = data.get("original_due_date") or p.get("to")
        case "parent_changed":
            new["parent"] = p.get("to")
        case "points_changed":
            new["points"] = p.get("to")
        case "title_changed":
            new["title"] = p.get("to")
        case "pr_merged":
            new["state"], new["merged_at"] = "merged", when
        case "pr_closed":
            new["state"], new["closed_at"] = "closed", when
        case "pr_reopened":
            new["state"], new["closed_at"] = "open", None
        case _:
            return data
    return new if new != data else data


def build_versions(events: Iterable[Event]) -> list[tuple[datetime, dict[str, Any]]]:
    """Versions for one entity. Several changes at the same instant collapse into one version."""
    versions: list[tuple[datetime, dict[str, Any]]] = []
    data: dict[str, Any] | None = None
    for event in sorted(events, key=sort_key):
        new = apply_event(data, event)
        if new is None or new is data:
            continue
        data = new
        if versions and versions[-1][0] == event.occurred_at:
            versions[-1] = (event.occurred_at, data)
        else:
            versions.append((event.occurred_at, data))
    return versions
