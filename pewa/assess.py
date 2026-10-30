"""LLM risk assessment per epic (SPEC §10.1), with citation rules enforced in code."""

import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from pewa import signals as signals_mod
from pewa import snapshot
from pewa.config import Settings, get_settings
from pewa.llm import LLM, LLMAuthError, LLMError
from pewa.models import Assessment, Run
from pewa.schemas import EpicState, Event, ProjectState, Signal, status_category

log = logging.getLogger(__name__)

PROMPTS = Path(__file__).parent / "prompts"
MAX_EVIDENCE = 40
MAX_SLACK = 12
EXCERPT_CHARS = 280

Rating = Literal["green", "amber", "red"]


class KeyRisk(BaseModel):
    summary: str
    evidence_ids: list[str] = Field(default_factory=list)


class AssessmentOutput(BaseModel):
    """What the model must return (validated strictly)."""

    rating: Rating
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1)
    predicted_slip_days: int | None = Field(default=None, ge=0)
    key_risks: list[KeyRisk] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)


# The same shape as AssessmentOutput, for the API's structured outputs. Ranges and the citation
# rules aren't expressible here, so replies are still validated in code (validate()).
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rating": {"type": "string", "enum": ["green", "amber", "red"]},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
        "predicted_slip_days": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "key_risks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"summary": {"type": "string"}, "evidence_ids": {"type": "array", "items": {"type": "string"}}},
                "required": ["summary", "evidence_ids"],
                "additionalProperties": False,
            },
        },
        "suggested_actions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["rating", "confidence", "rationale", "predicted_slip_days", "key_risks", "suggested_actions"],
    "additionalProperties": False,
}


class InvalidAssessment(ValueError):
    pass


class EvidenceItem(BaseModel):
    id: str
    when: str
    source: str
    kind: str
    summary: str
    url: str | None = None


class EpicPacket(BaseModel):
    """Everything the model sees for one epic."""

    as_of: datetime
    epic: EpicState
    business_days_left: int | None
    started_at: datetime | None = None  # when work on the epic began
    weeks_in_progress: float | None = None
    children: dict[str, Any]
    signals: list[Signal]
    evidence: list[EvidenceItem]

    @property
    def evidence_ids(self) -> set[str]:
        return {e.id for e in self.evidence}


class EpicAssessment(BaseModel):
    epic_key: str
    title: str
    rating: Literal["green", "amber", "red", "unknown"]
    confidence: float = 0.0
    rationale: str = ""
    predicted_slip_days: int | None = None
    key_risks: list[KeyRisk] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)  # every cited item
    signals: list[Signal] = Field(default_factory=list)
    dropped_evidence_ids: list[str] = Field(default_factory=list)
    attempts: int = 0
    error: str | None = None
    skipped: str | None = None
    guard: str | None = None  # why pewa overrode the model's rating, if it did


class RunResult(BaseModel):
    run_id: int
    project: str
    as_of: datetime
    model: str
    assessments: list[EpicAssessment]


# ----- building the input -----------------------------------------------------------------------


def _num(value: float | None) -> float | int | None:
    return int(value) if value is not None and float(value).is_integer() else value


def _short(text: str | None, limit: int = EXCERPT_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe(event: Event, state: ProjectState) -> str:
    """One line a human (or model) can read without the raw payload."""
    p = event.payload
    who = event.actor or "someone"
    if event.source == "jira":
        ticket = state.tickets.get(event.entity_id) or state.epics.get(event.entity_id)
        name = f"{event.entity_id} '{ticket.title}'" if ticket else event.entity_id
        if event.kind == "ticket_created" and p.get("issue_type") == "Epic":
            return f"{who} created epic {name} (owner {p.get('assignee')}, due {p.get('due_date') or 'not set'})"
        if event.kind == "ticket_created":
            parent = f" under {p['parent']}" if p.get("parent") else ""
            return f"{who} created {name}{parent} ({_num(p.get('points')) or '?'} pts, assignee {p.get('assignee')})"
        if "from" in p or "to" in p:
            field = event.kind.removesuffix("_changed").replace("_", " ")
            return f"{who} changed {field} of {name}: {p.get('from')} -> {p.get('to')}"
        return f"{who}: {event.kind.replace('_', ' ')} on {name}"
    if event.source == "github":
        pr = state.prs.get(event.entity_id)
        name = f"PR {event.entity_id} '{pr.title}'" if pr else f"PR {event.entity_id}"
        match event.kind:
            case "pr_opened":
                return f"{who} opened {name}"
            case "pr_review_requested":
                return f"{who} requested review of {name} from {p.get('reviewer')}"
            case "pr_reviewed":
                return f"{who} reviewed {name}: {p.get('state')}"
            case "ci_failed":
                return f"CI check '{p.get('check')}' failed on {name}"
            case _:
                return f"{who}: {event.kind.removeprefix('pr_')} {name}"
    channel = p.get("channel_name") or p.get("channel")
    reply = " (thread reply)" if p.get("thread_ts") and p.get("thread_ts") != p.get("ts") else ""
    return f"#{channel} {who}{reply}: \"{_short(p.get('text'))}\""


def select_evidence(state: ProjectState, epic: EpicState, signals: list[Signal]) -> list[Event]:
    """Up to MAX_EVIDENCE events: signal evidence first, then recent Slack, then recent activity."""
    by_id = {e.id: e for e in state.events}
    epic_events = state.epic_events(epic.key)
    chosen: dict[str, Event] = {}

    def take(events: list[Event], cap: int = MAX_EVIDENCE) -> None:
        for event in events:
            if len(chosen) >= MAX_EVIDENCE or cap <= 0:
                return
            if event.id and event.id not in chosen:
                chosen[event.id] = event
                cap -= 1

    for signal in sorted(signals, key=lambda s: -s.severity):
        take([by_id[i] for i in signal.evidence if i in by_id])
    take([e for e in reversed(epic_events) if e.source == "slack"], cap=MAX_SLACK)
    take(list(reversed(epic_events)))
    return sorted(chosen.values(), key=lambda e: (e.occurred_at, e.id or ""))


def build_packet(state: ProjectState, epic: EpicState, signals: list[Signal]) -> EpicPacket:
    children = state.epic_children(epic.key)
    keys = {epic.key, *epic.children}
    by_entity: dict[str, list[Event]] = {}
    for event in state.events:
        if event.entity_id in keys:
            by_entity.setdefault(event.entity_id, []).append(event)
    started = signals_mod.started_at(epic, by_entity)
    open_children = [t for t in children if t.category != "done"]
    counts = {c: sum(1 for t in children if t.category == c) for c in ("todo", "in_progress", "done")}
    due = date.fromisoformat(epic.due_date) if epic.due_date else None
    return EpicPacket(
        as_of=state.as_of,
        epic=epic,
        business_days_left=signals_mod.business_days(state.as_of, due) if due else None,
        started_at=started,
        weeks_in_progress=round(signals_mod.business_days(started, state.as_of) / 5, 1) if started else None,
        children={
            **counts,
            "points_done": _num(sum(t.points or 0 for t in children if t.category == "done")),
            "points_open": _num(sum(t.points or 0 for t in open_children)),
            "open": [
                {
                    "key": t.key, "title": t.title, "status": t.status, "assignee": t.assignee, "points": _num(t.points),
                    "last_activity": f"{t.last_activity_at:%Y-%m-%d}" if t.last_activity_at else "none",
                }
                for t in open_children
            ],
        },
        signals=signals,
        evidence=[
            EvidenceItem(id=e.id, when=f"{e.occurred_at:%Y-%m-%d %H:%M}", source=e.source, kind=e.kind,
                         summary=describe(e, state), url=e.url)
            for e in select_evidence(state, epic, signals)
            if e.id
        ],
    )


_env = Environment(loader=FileSystemLoader(PROMPTS), undefined=StrictUndefined, keep_trailing_newline=True)


def render_prompt(packet: EpicPacket) -> tuple[str, str]:
    system = (PROMPTS / "assess_system.md").read_text()
    cfg = get_settings()
    user = _env.get_template("assess_user.md.j2").render(
        as_of=f"{packet.as_of:%Y-%m-%d %H:%M} UTC",
        epic=packet.epic,
        business_days_left=packet.business_days_left,
        started=f"{packet.started_at:%Y-%m-%d}" if packet.started_at else None,
        weeks_in_progress=packet.weeks_in_progress,
        min_weeks=cfg.pewa_min_history_weeks,
        min_done=cfg.pewa_min_done_tickets,
        children=packet.children,
        signals=packet.signals,
        evidence=packet.evidence,
    )
    return system, user


# ----- validating the output --------------------------------------------------------------------


def parse_json(text: str) -> Any:
    """Parse a JSON object, tolerating code fences or stray text around it."""
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(stripped[start : end + 1])


def validate(text: str, allowed_ids: set[str]) -> tuple[AssessmentOutput, list[str]]:
    """Parse and check a model reply. Unknown evidence ids are dropped (and returned);
    raises InvalidAssessment if the reply is unusable."""
    try:
        output = AssessmentOutput.model_validate(parse_json(text))
    except json.JSONDecodeError as exc:
        raise InvalidAssessment(f"response is not valid JSON ({exc.msg} at char {exc.pos})") from exc
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'root'}: {e['msg']}" for e in exc.errors())
        raise InvalidAssessment(f"response does not match the schema: {problems}") from exc

    dropped: list[str] = []
    for risk in output.key_risks:
        dropped += [i for i in risk.evidence_ids if i not in allowed_ids]
        risk.evidence_ids = [i for i in dict.fromkeys(risk.evidence_ids) if i in allowed_ids]
    output.key_risks = [r for r in output.key_risks if r.evidence_ids]
    if output.rating in ("amber", "red") and not output.key_risks:
        detail = f" (unknown ids removed: {', '.join(dropped)})" if dropped else ""
        raise InvalidAssessment(
            f"a {output.rating} rating must cite at least one evidence id from the evidence list{detail}"
        )
    return output, dropped


# ----- thin-history guard -----------------------------------------------------------------------

GUARD_DUE_SOON_DAYS = 10  # within two working weeks of the due date, little history is itself a risk


def thin_history_guard(packet: EpicPacket, output: AssessmentOutput, cfg: Settings) -> str | None:
    """Why an amber/red rating should be kept green, or None.

    Backtests showed the model's early false alarms were pace projections from a week or two of
    work. Until an epic has `pewa_min_history_weeks` of history and `pewa_min_done_tickets` done
    tickets, amber/red needs a detector signal of severity 2+ behind it. Epics due soon are exempt."""
    if output.rating not in ("amber", "red") or cfg.pewa_min_history_weeks <= 0:
        return None
    if packet.business_days_left is not None and packet.business_days_left <= GUARD_DUE_SOON_DAYS:
        return None
    weeks, done = packet.weeks_in_progress or 0.0, packet.children.get("done", 0)
    if weeks >= cfg.pewa_min_history_weeks and done >= cfg.pewa_min_done_tickets:
        return None
    if any(s.severity >= 2 for s in packet.signals):
        return None
    history = (f"{weeks:g} weeks of work and {done} finished ticket(s)" if packet.started_at
               else "work hasn't started")
    return (f"the model rated it {output.rating}, but with {history} and no detector signal of severity 2+, "
            f"a pace projection is too early to flag (needs {cfg.pewa_min_history_weeks:g} weeks and "
            f"{cfg.pewa_min_done_tickets} done tickets)")


# ----- dry-run responder ------------------------------------------------------------------------

# How much each signal type counts toward a rating in the offline heuristic.
_WEIGHTS = {"reopened": 0.5, "ci_flaky": 0.5, "owner_overload": 0.5}
_ACTIONS = {
    "stale_review": "Escalate the pending review with the reviewer or their lead and agree a review date.",
    "stale_ticket": "Check in with the assignee on idle tickets: blocked, deprioritised, or just not updated?",
    "reopened": "Confirm the reopened work is understood and has an owner.",
    "due_date_pushed": "Confirm the new date with stakeholders and what it depends on.",
    "scope_growth": "Review the added scope with product: cut, defer, or move the date explicitly.",
    "ci_flaky": "Get the failing CI check fixed or quarantined by its owner.",
    "burn_gap": "Re-plan remaining work against the due date: cut scope or add capacity.",
    "owner_overload": "Rebalance in-progress work away from the overloaded owner.",
    "blocker_language": "Unblock the dependency named in Slack; set a decision deadline with the other team.",
}


def offline_response(packet: EpicPacket) -> str:
    """Deterministic stand-in for the model in dry-run mode: rates from signal severity.

    A transparent baseline, not the product. It goes through exactly the same validation as
    a real model reply."""
    score = sum(s.severity * _WEIGHTS.get(s.type, 1.0) for s in packet.signals)
    strong = [s for s in packet.signals if _WEIGHTS.get(s.type, 1.0) == 1.0]
    rating = "red" if score >= 6 else "amber" if score >= 3 or any(s.severity == 3 for s in strong) else "green"
    top = sorted(packet.signals, key=lambda s: (-s.severity * _WEIGHTS.get(s.type, 1.0), s.type))[:3]
    if rating == "green":
        rationale = ("No detector signals." if not packet.signals else
                     f"Only minor signals ({', '.join(sorted({s.type for s in packet.signals}))}); nothing threatens the due date yet.")
        risks: list[dict[str, Any]] = []
    else:
        rationale = (f"{len(packet.signals)} risk signals (score {score:g}). "
                     f"Most serious: {top[0].detail}")
        risks = [{"summary": s.detail, "evidence_ids": s.evidence[:5]} for s in top if s.evidence]
    return json.dumps({
        "rating": rating,
        "confidence": round(min(0.9, 0.6 + 0.05 * score), 2),
        "rationale": rationale,
        "predicted_slip_days": None,
        "key_risks": risks,
        "suggested_actions": list(dict.fromkeys(_ACTIONS[s.type] for s in top if s.type in _ACTIONS))[:3] if risks else [],
    })


# ----- running ----------------------------------------------------------------------------------


def assess_epic(llm: LLM, state: ProjectState, epic: EpicState, signals: list[Signal],
                cfg: Settings | None = None) -> EpicAssessment:
    base = EpicAssessment(epic_key=epic.key, title=epic.title, rating="unknown", signals=signals)
    if status_category(epic.status) == "done":
        when = f"{epic.resolved_at:%Y-%m-%d}" if epic.resolved_at else "an earlier date"
        return base.model_copy(update={"rating": "green", "confidence": 1.0, "rationale": f"Completed on {when}.",
                                       "skipped": "done"})

    packet = build_packet(state, epic, signals)
    system, user = render_prompt(packet)
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    error = None
    for attempt in (1, 2):
        try:
            text = llm.complete("assess", system, messages, canned=lambda: offline_response(packet),
                                output_schema=OUTPUT_SCHEMA)
        except LLMAuthError:
            raise  # no point trying the remaining epics
        except LLMError as exc:
            return base.model_copy(update={"attempts": attempt, "error": str(exc)})
        try:
            output, dropped = validate(text, packet.evidence_ids)
        except InvalidAssessment as exc:
            error = str(exc)
            log.warning("assessment of %s invalid (attempt %d): %s", epic.key, attempt, error)
            messages += [
                {"role": "assistant", "content": text or "(empty)"},
                {"role": "user", "content": f"That response was invalid: {error}. "
                                            "Reply with the corrected JSON object only."},
            ]
            continue
        by_id = {e.id: e for e in packet.evidence}
        cited = list(dict.fromkeys(i for r in output.key_risks for i in r.evidence_ids))
        update: dict[str, Any] = {**output.model_dump(), "key_risks": output.key_risks}
        if guard := thin_history_guard(packet, output, cfg or get_settings()):
            # Keep the model's reasoning and risks visible as things to watch; only the rating changes.
            update.update(rating="green", guard=guard,
                          rationale=f"{output.rationale} (Kept green: {guard}.)",
                          predicted_slip_days=None)
        return base.model_copy(update={
            **update,
            "evidence": [by_id[i] for i in cited],
            "dropped_evidence_ids": dropped,
            "attempts": attempt,
        })
    return base.model_copy(update={"attempts": 2, "error": error})


def run(
    session: Session,
    project: str,
    as_of: datetime,
    llm: LLM,
    *,
    mode: str = "live",
    label: str | None = None,
    cfg: Settings | None = None,
    state: ProjectState | None = None,
) -> RunResult:
    """Snapshot -> signals -> one assessment per epic, stored as a run.

    Pass `state` to reuse a snapshot already built for `as_of`."""
    state = state or snapshot.build(session, project, as_of)
    detected = signals_mod.detect(state, cfg or get_settings())
    results = [assess_epic(llm, state, epic, detected.get(key, []), cfg) for key, epic in state.epics.items()]

    run_row = Run(project=project, as_of=state.as_of, mode=mode, model=llm.label, label=label)
    session.add(run_row)
    session.flush()
    for r in results:
        session.add(Assessment(
            run_id=run_row.id,
            epic_key=r.epic_key,
            rating=r.rating,
            confidence=r.confidence,
            rationale=r.rationale if r.rating != "unknown" else f"Assessment failed: {r.error}",
            predicted_slip_days=r.predicted_slip_days,
            evidence=[e.model_dump() for e in r.evidence],
            signals=[s.model_dump() for s in r.signals],
            detail={
                "title": r.title,
                "owner": state.epics[r.epic_key].owner,
                "due_date": state.epics[r.epic_key].due_date,
                "key_risks": [k.model_dump() for k in r.key_risks],
                "suggested_actions": r.suggested_actions,
                "dropped_evidence_ids": r.dropped_evidence_ids,
                "attempts": r.attempts,
                "error": r.error,
                "skipped": r.skipped,
                "guard": r.guard,
            },
        ))
    session.flush()
    return RunResult(run_id=run_row.id, project=project, as_of=state.as_of, model=llm.label, assessments=results)
