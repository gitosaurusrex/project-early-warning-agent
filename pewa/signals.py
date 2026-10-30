"""Deterministic risk detectors (SPEC §9). Cheap, explainable, and the LLM's main input.

Every detector looks only at a ProjectState, so it inherits the snapshot's no-future-data
guarantee. Each returns Signals whose evidence is a list of event ids linked to the epic.
"""

import math
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from pewa.config import Settings, get_settings
from pewa.schemas import WORKFLOW, EpicState, Event, ProjectState, Signal, status_category

# burn_gap: how much longer than the time left the projected finish must be (needed / left).
BURN_RATIOS = (1.25, 1.75, 2.5)


def business_days(start: datetime | date, end: datetime | date) -> int:
    """Weekdays after `start`'s date up to and including `end`'s date (0 if end <= start)."""
    a = start.date() if isinstance(start, datetime) else start
    b = end.date() if isinstance(end, datetime) else end
    if b <= a:
        return 0
    full_weeks, extra = divmod((b - a).days, 7)
    count = full_weeks * 5
    for i in range(1, extra + 1):
        if (a + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


def scale(value: float, threshold: float) -> int:
    """Severity for a value already over `threshold`: 1 up to 1.5x, 2 up to 2.5x, then 3."""
    if value <= threshold * 1.5:
        return 1
    return 2 if value <= threshold * 2.5 else 3


@dataclass
class Context:
    state: ProjectState
    cfg: Settings
    by_entity: dict[str, list[Event]] = field(default_factory=lambda: defaultdict(list))

    def __post_init__(self) -> None:
        for event in self.state.events:
            self.by_entity[event.entity_id].append(event)

    @property
    def as_of(self) -> datetime:
        return self.state.as_of

    @property
    def lookback_start(self) -> datetime:
        return self.as_of - timedelta(days=self.cfg.pewa_signal_lookback_days)

    def last_event(self, entity_id: str, kind: str, at: datetime | None = None) -> Event | None:
        matches = [e for e in self.by_entity[entity_id] if e.kind == kind and (at is None or e.occurred_at == at)]
        return matches[-1] if matches else None

    def started_at(self, epic: EpicState) -> datetime | None:
        return started_at(epic, self.by_entity)


def started_at(epic: EpicState, by_entity: dict[str, list[Event]]) -> datetime | None:
    """When the epic (or, failing that, its first child) moved into progress."""
    for event in by_entity.get(epic.key, []):
        if event.kind == "status_changed" and status_category(event.payload.get("to")) != "todo":
            return event.occurred_at
    starts = [
        e.occurred_at
        for child in epic.children
        for e in by_entity.get(child, [])
        if e.kind == "status_changed" and status_category(e.payload.get("to")) != "todo"
    ]
    return min(starts, default=None)


def _ids(events: list[Event | None]) -> list[str]:
    return [e.id for e in events if e is not None and e.id]


def stale_review(ctx: Context, epic: EpicState) -> list[Signal]:
    threshold = ctx.cfg.pewa_stale_review_days
    out = []
    for pr in ctx.state.epic_prs(epic.key):
        if pr.state != "open" or not pr.review_requests:
            continue
        request = pr.review_requests[-1]
        if any(review.at >= request.at for review in pr.reviews):
            continue  # reviewed since the latest request; ball is with the author
        waited = business_days(request.at, ctx.as_of)
        if waited <= threshold:
            continue
        evidence = _ids([ctx.last_event(pr.key, "pr_review_requested", request.at), ctx.last_event(pr.key, "pr_opened")])
        out.append(Signal(
            type="stale_review", severity=scale(waited, threshold), epic_key=epic.key, evidence=evidence,
            detail=f"PR {pr.key} '{pr.title}' has waited {waited} business days for review from {request.reviewer}.",
        ))
    return out


def stale_ticket(ctx: Context, epic: EpicState) -> list[Signal]:
    threshold = ctx.cfg.pewa_stale_ticket_days
    out = []
    for ticket in ctx.state.epic_children(epic.key):
        if ticket.category != "in_progress":
            continue
        idle = business_days(ticket.last_activity_at or ticket.created_at, ctx.as_of)
        if idle <= threshold:
            continue
        evidence = [ticket.last_activity_event_id] if ticket.last_activity_event_id else []
        out.append(Signal(
            type="stale_ticket", severity=scale(idle, threshold), epic_key=epic.key, evidence=evidence,
            detail=f"{ticket.key} '{ticket.title}' is {ticket.status} with no linked activity for {idle} business days"
                   f" (assignee: {ticket.assignee or 'unassigned'}).",
        ))
    return out


def reopened(ctx: Context, epic: EpicState) -> list[Signal]:
    moves: dict[str, list[Event]] = defaultdict(list)
    for event in ctx.state.epic_events(epic.key):
        if event.kind != "status_changed" or event.occurred_at < ctx.lookback_start:
            continue
        before, after = WORKFLOW.get(event.payload.get("from"), 0), WORKFLOW.get(event.payload.get("to"), 0)
        if after < before:
            moves[event.entity_id].append(event)
    out = []
    for key, events in moves.items():
        from_done = any(status_category(e.payload.get("from")) == "done" for e in events)
        severity = min(3, (2 if from_done else 1) + len(events) - 1)
        steps = ", ".join(f"{e.payload.get('from')} -> {e.payload.get('to')} on {e.occurred_at:%b %d}" for e in events)
        out.append(Signal(
            type="reopened", severity=severity, epic_key=epic.key, evidence=_ids(events),
            detail=f"{key} moved backwards in the workflow: {steps}.",
        ))
    return out


def due_date_pushed(ctx: Context, epic: EpicState) -> list[Signal]:
    pushes = []
    total_days = 0
    for event in ctx.state.epic_events(epic.key):
        if event.kind != "due_date_changed" or not event.payload.get("from") or not event.payload.get("to"):
            continue
        delta = (date.fromisoformat(event.payload["to"]) - date.fromisoformat(event.payload["from"])).days
        if delta > 0:
            pushes.append(event)
            total_days += delta
    if not pushes:
        return []
    return [Signal(
        type="due_date_pushed", severity=2 if total_days <= 7 else 3, epic_key=epic.key, evidence=_ids(pushes),
        detail=f"Due date pushed {len(pushes)} time(s), {total_days} days later in total"
               f" (originally {epic.original_due_date}, now {epic.due_date}).",
    )]


def scope_growth(ctx: Context, epic: EpicState) -> list[Signal]:
    started = ctx.started_at(epic)
    if started is None:
        return []
    joined: dict[str, Event] = {}
    for child in epic.children:
        for event in ctx.by_entity[child]:
            if (event.kind == "ticket_created" and event.payload.get("parent") == epic.key) or (
                event.kind == "parent_changed" and event.payload.get("to") == epic.key
            ):
                joined[child] = event
    added = [e for e in joined.values() if e.occurred_at > started]
    baseline = len(epic.children) - len(added)
    if not added or baseline <= 0:
        return []
    pct = 100 * len(added) / baseline
    threshold = ctx.cfg.pewa_scope_growth_pct
    if pct <= threshold:
        return []
    return [Signal(
        type="scope_growth", severity=scale(pct, threshold), epic_key=epic.key, evidence=_ids(added),
        detail=f"{len(added)} child tickets added after the epic started on {started:%b %d}"
               f" ({baseline} -> {len(epic.children)}, +{pct:.0f}%): {', '.join(e.entity_id for e in added)}.",
    )]


def ci_flaky(ctx: Context, epic: EpicState) -> list[Signal]:
    threshold = ctx.cfg.pewa_ci_flaky_failures
    out = []
    for pr in ctx.state.epic_prs(epic.key):
        failures = [e for e in ctx.by_entity[pr.key] if e.kind == "ci_failed" and e.occurred_at >= ctx.lookback_start]
        if len(failures) < threshold:
            continue
        out.append(Signal(
            type="ci_flaky", severity=1 if len(failures) < 2 * threshold else 2, epic_key=epic.key,
            evidence=_ids(failures),
            detail=f"CI failed {len(failures)} times on {pr.key} '{pr.title}' in the last"
                   f" {ctx.cfg.pewa_signal_lookback_days} days ({pr.state}).",
        ))
    return out


def burn_gap(ctx: Context, epic: EpicState) -> list[Signal]:
    if not epic.due_date:
        return []
    children = ctx.state.epic_children(epic.key)
    remaining = sum(t.points or 1 for t in children if t.category != "done")
    started = ctx.started_at(epic)
    if remaining == 0 or started is None or business_days(started, ctx.as_of) < 5:
        return []  # nothing left, or less than a week of history to judge pace from

    window_start = max(started, ctx.as_of - timedelta(weeks=ctx.cfg.pewa_burn_window_weeks))
    done = sum(t.points or 1 for t in children if t.resolved_at and window_start < t.resolved_at <= ctx.as_of)
    weeks_observed = max(business_days(window_start, ctx.as_of), 1) / 5
    per_week = done / weeks_observed
    due = date.fromisoformat(epic.due_date)
    weeks_left = business_days(ctx.as_of, due) / 5
    evidence = _ids([ctx.last_event(epic.key, "due_date_changed") or ctx.last_event(epic.key, "ticket_created")])

    if weeks_left <= 0:
        severity, ratio = 3, math.inf
        detail = f"Past due date {epic.due_date} with {remaining:g} points still open."
    else:
        needed = remaining / per_week if per_week else math.inf
        ratio = needed / weeks_left
        if ratio <= BURN_RATIOS[0]:
            return []
        severity = 1 if ratio <= BURN_RATIOS[1] else 2 if ratio <= BURN_RATIOS[2] else 3
        pace = f"~{needed:.1f} weeks at the trailing pace" if per_week else "no points completed recently"
        detail = (f"{remaining:g} points open with {weeks_left:.1f} weeks to the {epic.due_date} due date;"
                  f" trailing throughput is {per_week:.1f} points/week ({pace}).")
    return [Signal(type="burn_gap", severity=severity, epic_key=epic.key, evidence=evidence, detail=detail)]


def owner_overload(ctx: Context, epic: EpicState) -> list[Signal]:
    limit = ctx.cfg.pewa_owner_overload_n
    load = Counter(t.assignee for t in ctx.state.tickets.values() if t.category == "in_progress" and t.assignee)
    out = []
    for owner, count in load.items():
        if count <= limit:
            continue
        mine = [t for t in ctx.state.epic_children(epic.key) if t.assignee == owner and t.category == "in_progress"]
        if not mine:
            continue
        evidence = _ids([ctx.last_event(t.key, "status_changed") for t in mine])
        out.append(Signal(
            type="owner_overload", severity=1 if count <= limit + 2 else 2, epic_key=epic.key, evidence=evidence,
            detail=f"{owner} has {count} tickets in progress across epics ({len(mine)} in this epic:"
                   f" {', '.join(t.key for t in mine)}).",
        ))
    return out


def blocker_language(ctx: Context, epic: EpicState) -> list[Signal]:
    phrases = [p.lower() for p in ctx.cfg.pewa_blocker_phrases]
    hits = [
        e for e in ctx.state.epic_events(epic.key)
        if e.source == "slack" and e.occurred_at >= ctx.lookback_start
        and any(p in (e.payload.get("text") or "").lower() for p in phrases)
    ]
    if not hits:
        return []
    severity = 1 if len(hits) == 1 else 2 if len(hits) <= 3 else 3
    latest = (hits[-1].payload.get("text") or "")[:160]
    return [Signal(
        type="blocker_language", severity=severity, epic_key=epic.key, evidence=_ids(hits[-10:]),
        detail=f"{len(hits)} linked Slack message(s) in the last {ctx.cfg.pewa_signal_lookback_days} days use"
               f" blocker language; latest from {hits[-1].actor}: \"{latest}\"",
    )]


DETECTORS: dict[str, Callable[[Context, EpicState], list[Signal]]] = {
    "stale_review": stale_review,
    "stale_ticket": stale_ticket,
    "reopened": reopened,
    "due_date_pushed": due_date_pushed,
    "scope_growth": scope_growth,
    "ci_flaky": ci_flaky,
    "burn_gap": burn_gap,
    "owner_overload": owner_overload,
    "blocker_language": blocker_language,
}


def detect(state: ProjectState, cfg: Settings | None = None) -> dict[str, list[Signal]]:
    """Signals for every epic that is not done, keyed by epic key."""
    ctx = Context(state, cfg or get_settings())
    return {
        key: [signal for detector in DETECTORS.values() for signal in detector(ctx, epic)]
        for key, epic in state.epics.items()
        if status_category(epic.status) != "done"
    }
