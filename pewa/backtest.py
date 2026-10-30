"""Replay a project step by step and score its ratings against known outcomes (SPEC §12).

Each step is an ordinary assessment run (mode "backtest") on a snapshot that holds only the
data that existed at that moment. Metrics are computed from the stored runs, so a backtest
can be re-scored or compared at any time.

An epic counts as *flagged* when it was rated amber or red at a step strictly before its
original planned date. Ratings after that date don't count: an open epic past its due date is
late by definition, and calling that an early warning would inflate recall.
"""

import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pewa import assess, snapshot
from pewa.assess import RunResult
from pewa.llm import LLM
from pewa.models import Assessment, Backtest, BacktestStep, EventRow, Feedback, LLMCall, Outcome, Run

TEMPLATES = Path(__file__).parent / "report_templates"
FLAGS = ("amber", "red")
STEP_RE = re.compile(r"^\s*(\d+)\s*([dw])\s*$")


def parse_step(value: str) -> int:
    """'7d' or '1w' -> days."""
    match = STEP_RE.match(value.lower())
    if not match or int(match[1]) == 0:
        raise ValueError(f"step must look like '7d' or '1w', got {value!r}")
    return int(match[1]) * (7 if match[2] == "w" else 1)


def step_dates(start: date, end: date, step_days: int) -> list[date]:
    if end < start:
        raise ValueError(f"--to ({end}) is before --from ({start})")
    return [start + timedelta(days=i) for i in range(0, (end - start).days + 1, step_days)]


def event_range(session: Session, project: str) -> tuple[date, date] | None:
    first, last = session.execute(select(func.min(EventRow.occurred_at), func.max(EventRow.occurred_at))
                                  .where(EventRow.project == project)).one()
    return (first.date(), last.date()) if first else None


# ----- running ----------------------------------------------------------------------------------


def run(
    session: Session,
    project: str,
    start: date,
    end: date,
    step_days: int,
    llm: LLM,
    *,
    label: str | None = None,
    on_step: Callable[[date, RunResult], None] | None = None,
) -> Backtest:
    """Assess the project as of each step date and store the runs as one backtest."""
    dates = step_dates(start, end, step_days)
    calls_before = session.scalar(select(func.max(LLMCall.id))) or 0
    bt = Backtest(project=project, start=start, end=end, step_days=step_days, model=llm.label, label=label)
    session.add(bt)
    session.flush()
    for day in dates:
        when = snapshot.parse_as_of(day)
        state = snapshot.build(session, project, when)
        result = assess.run(session, project, when, llm, mode="backtest", label=label or f"backtest {bt.id}",
                            state=state)
        session.add(BacktestStep(backtest_id=bt.id, run_id=result.run_id, as_of=result.as_of))
        bt.llm_calls, bt.cost_usd = session.execute(
            select(func.count(LLMCall.id), func.coalesce(func.sum(LLMCall.cost_usd), 0.0))
            .where(LLMCall.id > calls_before)).one()
        session.flush()
        if on_step:
            on_step(day, result)
    return bt


# ----- scoring ----------------------------------------------------------------------------------


class Cell(BaseModel):
    as_of: date
    rating: str
    assessment_id: int
    confidence: float = 0.0
    rationale: str = ""
    done: bool = False


class EpicResult(BaseModel):
    key: str
    title: str
    planned_date: date | None = None
    actual_date: date | None = None
    slipped: bool | None = None  # None: no ground truth for this epic
    cells: list[Cell] = Field(default_factory=list)
    first_flag: date | None = None  # first amber/red before the planned date
    first_red: date | None = None
    lead_time_days: int | None = None  # planned date - first flag, for slipped epics
    # Start of the unbroken amber/red streak still running at the last step before the planned
    # date; None if that step wasn't flagged. A warning later withdrawn doesn't count here.
    sustained_flag: date | None = None
    sustained_lead_time_days: int | None = None

    @property
    def flagged(self) -> bool:
        return self.first_flag is not None

    @property
    def outcome(self) -> str:
        if self.slipped is None:
            return "no ground truth"
        if not self.slipped:
            return "on time"
        days = (self.actual_date - self.planned_date).days if self.actual_date and self.planned_date else None
        return f"slipped {days} days" if days is not None else "slipped"

    @property
    def verdict(self) -> str:
        if self.slipped is None:
            return ""
        if self.slipped:
            return f"flagged {self.lead_time_days} days early" if self.flagged else "missed"
        return "false alarm" if self.flagged else "correctly quiet"


class Evaluation(BaseModel):
    backtest_id: int
    project: str
    model: str
    label: str | None
    start: date
    end: date
    step_days: int
    created_at: datetime
    steps: list[date]
    epics: list[EpicResult]
    assessments: int = 0
    unknown_assessments: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    citations_total: int = 0
    citations_valid: int = 0
    citations_dropped: int = 0
    feedback_useful: int = 0
    feedback_noise: int = 0
    llm_calls: int = 0
    cost_usd: float = 0.0

    @property
    def name(self) -> str:
        return f"Backtest {self.backtest_id}" + (f" ({self.label})" if self.label else "")

    @property
    def span(self) -> str:
        if not self.steps:
            return f"no steps between {self.start:%d %b %Y} and {self.end:%d %b %Y}"
        return (f"{len(self.steps)} steps every {self.step_days} days, "
                f"{self.steps[0]:%a %d %b} to {self.steps[-1]:%a %d %b %Y}")

    @property
    def scored(self) -> list[EpicResult]:
        return [e for e in self.epics if e.slipped is not None]

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def false_alarm_rate(self) -> float | None:
        return self.fp / (self.fp + self.tn) if self.fp + self.tn else None

    @property
    def lead_times(self) -> list[int]:
        return [e.lead_time_days for e in self.epics if e.lead_time_days is not None]

    @property
    def lead_time_mean(self) -> float | None:
        return mean(self.lead_times) if self.lead_times else None

    @property
    def sustained_lead_times(self) -> list[int]:
        return [e.sustained_lead_time_days for e in self.epics if e.sustained_lead_time_days is not None]

    @property
    def citation_validity(self) -> float | None:
        return self.citations_valid / self.citations_total if self.citations_total else None

    def metric_rows(self) -> list[tuple[str, str, str]]:
        """(metric, value, how it was computed), pre-formatted for the Markdown and HTML reports."""
        def pct(value: float | None, num: int, den: int) -> str:
            return "n/a" if value is None else f"{value:.0%} ({num}/{den})"

        def days(values: list[int]) -> str:
            return f"{mean(values):.1f} days (min {min(values)}, max {max(values)})" if values else "n/a"

        slipped = self.tp + self.fn
        on_time = self.fp + self.tn
        return [
            ("Lead time", days(self.lead_times), "Planned date minus first amber/red, for slipped epics that were flagged."),
            ("Sustained lead time", days(self.sustained_lead_times),
             "Planned date minus the start of the amber/red streak still running at the last step before it."),
            ("Recall", pct(self.recall, self.tp, slipped),
             "Slipped epics flagged amber/red at some step before their planned date."),
            ("Precision", pct(self.precision, self.tp, self.tp + self.fp),
             "Flagged epics (before their planned date) that actually slipped."),
            ("False alarm rate", pct(self.false_alarm_rate, self.fp, on_time),
             "On-time epics that were flagged anyway."),
            ("Citation validity", pct(self.citation_validity, self.citations_valid, self.citations_total),
             "Cited evidence ids that exist and are linked to the epic as of the step, "
             f"counting {self.citations_dropped} made-up id(s) the validator dropped."),
            ("Failed assessments", f"{self.unknown_assessments} of {self.assessments}",
             "Stored as 'unknown' after a second invalid reply or an API error."),
            ("Feedback", f"{self.feedback_useful} useful, {self.feedback_noise} noise",
             "TPM verdicts recorded on this backtest's assessments."),
            ("Model calls", f"{self.llm_calls} (${self.cost_usd:.2f})", "Estimated from token counts."),
        ]


def evaluate(session: Session, bt: Backtest) -> Evaluation:
    steps = list(session.execute(
        select(BacktestStep.as_of, Run).join(Run, Run.id == BacktestStep.run_id)
        .where(BacktestStep.backtest_id == bt.id).order_by(BacktestStep.as_of)
    ).all())
    outcomes = {o.epic_key: o for o in session.scalars(select(Outcome).where(Outcome.project == bt.project))}
    ev = Evaluation(backtest_id=bt.id, project=bt.project, model=bt.model, label=bt.label, start=bt.start, end=bt.end,
                    step_days=bt.step_days, created_at=bt.created_at, steps=[as_of.date() for as_of, _ in steps],
                    epics=[], llm_calls=bt.llm_calls, cost_usd=bt.cost_usd)
    epics: dict[str, EpicResult] = {}
    assessment_ids: list[int] = []

    for as_of, run in steps:
        linked = {e.id: e.epic_key for e in snapshot.build(session, bt.project, as_of).events}
        for a in session.scalars(select(Assessment).where(Assessment.run_id == run.id)):
            detail = a.detail or {}
            assessment_ids.append(a.id)
            ev.assessments += 1
            ev.unknown_assessments += a.rating == "unknown"
            cited = {i for r in detail.get("key_risks", []) for i in r.get("evidence_ids", [])}
            dropped = detail.get("dropped_evidence_ids") or []
            ev.citations_total += len(cited) + len(dropped)
            ev.citations_dropped += len(dropped)
            ev.citations_valid += sum(1 for i in cited if linked.get(i) == a.epic_key)

            outcome = outcomes.get(a.epic_key)
            epic = epics.setdefault(a.epic_key, EpicResult(
                key=a.epic_key, title=detail.get("title") or a.epic_key,
                planned_date=outcome.planned_date if outcome else None,
                actual_date=outcome.actual_date if outcome else None,
                slipped=outcome.slipped if outcome else None,
            ))
            epic.cells.append(Cell(as_of=as_of.date(), rating=a.rating, assessment_id=a.id, confidence=a.confidence,
                                   rationale=a.rationale, done=detail.get("skipped") == "done"))

    for epic in epics.values():
        before_due = [c for c in epic.cells if epic.planned_date is None or c.as_of < epic.planned_date]
        epic.first_flag = next((c.as_of for c in before_due if c.rating in FLAGS), None)
        epic.first_red = next((c.as_of for c in epic.cells if c.rating == "red"), None)
        streak: list[Cell] = []
        for c in before_due:
            streak = streak + [c] if c.rating in FLAGS else []
        epic.sustained_flag = streak[0].as_of if streak else None
        if epic.slipped and epic.first_flag and epic.planned_date:
            epic.lead_time_days = (epic.planned_date - epic.first_flag).days
        if epic.slipped and epic.sustained_flag and epic.planned_date:
            epic.sustained_lead_time_days = (epic.planned_date - epic.sustained_flag).days
        if epic.slipped is not None:
            ev.tp += epic.slipped and epic.flagged
            ev.fn += epic.slipped and not epic.flagged
            ev.fp += (not epic.slipped) and epic.flagged
            ev.tn += (not epic.slipped) and not epic.flagged

    ev.epics = sorted(epics.values(), key=lambda e: snapshot.natural_key(e.key))
    if assessment_ids:
        verdicts = dict(session.execute(select(Feedback.verdict, func.count(Feedback.id))
                                        .where(Feedback.assessment_id.in_(assessment_ids))
                                        .group_by(Feedback.verdict)).all())
        ev.feedback_useful, ev.feedback_noise = verdicts.get("useful", 0), verdicts.get("noise", 0)
    return ev


def list_backtests(session: Session, project: str) -> list[Backtest]:
    return list(session.scalars(select(Backtest).where(Backtest.project == project).order_by(Backtest.id.desc())))


# ----- chart geometry ---------------------------------------------------------------------------

LABEL_W, CELL_W, GAP, ROW_H, ROW_GAP, OUTCOME_W, TOP, AXIS_H = 190, 46, 6, 30, 10, 200, 8, 26
PITCH = CELL_W + GAP
CELL_TEXT = {"green": "G", "amber": "A", "red": "R", "unknown": "?"}


def chart(ev: Evaluation, link: bool = False) -> dict[str, Any]:
    """Positions for the rating-strip chart: one row per epic, one cell per step, with the
    planned and actual delivery dates marked on the same time scale."""
    if not ev.steps:
        return {"width": 0, "height": 0, "rows": [], "ticks": []}
    first = ev.steps[0]

    def x_of(day: date) -> float:
        return LABEL_W + (day - first).days / ev.step_days * PITCH + CELL_W / 2

    plot_right = LABEL_W + (len(ev.steps) + 0.6) * PITCH
    rows = []
    for i, epic in enumerate(ev.epics):
        y = TOP + i * (ROW_H + ROW_GAP)
        by_day = {c.as_of: c for c in epic.cells}
        cells = []
        for j, day in enumerate(ev.steps):
            c = by_day.get(day)
            if c is None:
                continue
            kind = "done" if c.done else c.rating
            status = "completed" if c.done else f"{c.rating} ({c.confidence:.0%})"
            cells.append({
                "x": LABEL_W + j * PITCH, "kind": kind, "text": "✓" if c.done else CELL_TEXT.get(c.rating, "?"),
                "href": f"/assessments/{c.assessment_id}" if link else None,
                "title": f"{epic.key} as of {day:%a %d %b}: {status}. {c.rationale}",
            })

        def marker(day: date | None) -> float | None:
            return min(max(x_of(day), LABEL_W - 2), plot_right) if day else None

        rows.append({"epic": epic, "y": y, "cells": cells,
                     "planned_x": marker(epic.planned_date), "actual_x": marker(epic.actual_date)})
    height = TOP + len(rows) * (ROW_H + ROW_GAP) + AXIS_H
    ticks = [{"x": LABEL_W + j * PITCH + CELL_W / 2, "text": f"{day:%d %b}"} for j, day in enumerate(ev.steps)]
    return {"width": plot_right + OUTCOME_W, "height": height, "rows": rows, "ticks": ticks,
            "cell_w": CELL_W, "row_h": ROW_H, "label_w": LABEL_W, "outcome_x": plot_right + 8,
            "axis_y": height - AXIS_H + 16}


# ----- reports ----------------------------------------------------------------------------------

_env = Environment(loader=FileSystemLoader(TEMPLATES), undefined=StrictUndefined, trim_blocks=True,
                   lstrip_blocks=True, autoescape=lambda name: bool(name and name.endswith((".html", ".html.j2"))))
_env.globals.update(chart=chart)

CELL_MD = {"green": "🟢G", "amber": "🟡A", "red": "🔴R", "unknown": "⚪?", "done": "✅"}


def _cells_md(epic: EpicResult, steps: list[date]) -> list[str]:
    by_day = {c.as_of: c for c in epic.cells}
    return [("" if (c := by_day.get(d)) is None else CELL_MD["done" if c.done else c.rating]) for d in steps]


_env.globals.update(cells_md=_cells_md, zip=zip)


def render(ev: Evaluation, fmt: str) -> str:
    return _env.get_template(f"backtest.{fmt}.j2").render(ev=ev, evs=[ev]).rstrip() + "\n"


def render_compare(a: Evaluation, b: Evaluation, fmt: str) -> str:
    return _env.get_template(f"compare.{fmt}.j2").render(a=a, b=b, evs=[a, b]).rstrip() + "\n"


def write_reports(out_dir: Path, stem: str, markdown: str, html: str) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = [out_dir / f"{stem}.md", out_dir / f"{stem}.html"]
    paths[0].write_text(markdown)
    paths[1].write_text(html)
    return paths
