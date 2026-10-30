"""Local dashboard (SPEC §13): overview, epic detail with feedback, and report drafts.

Server-rendered with Jinja2. HTMX (from a CDN) swaps fragments in place; every form also works
as a plain POST + redirect, so the dashboard stays usable offline.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from pewa import assess, backtest, report, runs, snapshot
from pewa.config import get_settings
from pewa.db import make_engine, session_scope
from pewa.llm import LLM, LLMAuthError
from pewa.models import Assessment, Backtest, BacktestStep, EventRow, Feedback, Run

TEMPLATES = Path(__file__).parent / "templates"
VERDICTS = ("useful", "noise")


def evidence_href(event_id: str, url: str | None) -> str:
    """Real Jira/GitHub/Slack links open the source; fixture (.example) links open the local view."""
    host = (urlparse(url).hostname or "") if url else ""
    return url if url and host and not host.endswith(".example") else f"/events/{event_id}"


# Backtest charts and tables are shared with the standalone reports in pewa/report_templates.
templates = Jinja2Templates(directory=[TEMPLATES, backtest.TEMPLATES])
templates.env.globals.update(evidence_href=evidence_href, verdicts=VERDICTS, chart=backtest.chart)
templates.env.filters["dt"] = lambda value, fmt="%a %d %b %Y": value.strftime(fmt) if value else ""


def get_session(request: Request) -> Iterator[Session]:
    with session_scope(request.app.state.engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _llm(request: Request, session: Session) -> LLM:
    return LLM(get_settings().pewa_model, dry_run=request.app.state.dry_run, session=session)


def _run_or_404(session: Session, project: str, run_id: int) -> Run:
    run = session.get(Run, run_id)
    if run is None or run.project != project:
        raise HTTPException(404, f"no run {run_id} for project {project!r}")
    return run


def _feedback(session: Session, assessment_ids: list[int]) -> dict[int, list[Feedback]]:
    rows = session.scalars(select(Feedback).where(Feedback.assessment_id.in_(assessment_ids))
                           .order_by(Feedback.created_at.desc(), Feedback.id.desc()))
    grouped: dict[int, list[Feedback]] = {i: [] for i in assessment_ids}
    for row in rows:
        grouped[row.assessment_id].append(row)
    return grouped


def _backtest_or_404(session: Session, project: str, backtest_id: int) -> Backtest:
    bt = session.get(Backtest, backtest_id)
    if bt is None or bt.project != project:
        raise HTTPException(404, f"no backtest {backtest_id} for project {project!r}")
    return bt


def create_app(engine: Engine | None = None, *, project: str | None = None, dry_run: bool = False) -> FastAPI:
    app = FastAPI(title="pewa dashboard", docs_url=None, redoc_url=None)
    app.state.engine = engine or make_engine(get_settings().pewa_db_path)
    app.state.project = project or get_settings().pewa_project
    app.state.dry_run = dry_run

    def page(request: Request, name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
        context.update(project=app.state.project, dry_run=app.state.dry_run)
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request, session: SessionDep, run: int | None = None, as_of: str | None = None) -> Response:
        project, notice = app.state.project, None
        if run is not None:
            current = _run_or_404(session, project, run)
        elif as_of:
            try:
                wanted = snapshot.parse_as_of(as_of)
            except ValueError as exc:
                raise HTTPException(400, f"bad date {as_of!r}") from exc
            current = runs.latest_run(session, project, wanted)
            if current is None or current.as_of.date() != wanted.date():
                earlier = f"; showing the latest earlier run ({current.as_of:%d %b})" if current else ""
                notice = f"No assessment as of {wanted:%a %d %b %Y}{earlier}."
        else:
            current = runs.latest_run(session, project)
        if current is None:
            return page(request, "overview.html", run=None, rows=[], all_runs=[], previous=None, notice=notice,
                        as_of_value=as_of or "")

        previous = runs.previous_run(session, current)
        before = {a.epic_key: a.rating for a in runs.assessments(session, previous.id)} if previous else {}
        assessments = runs.assessments(session, current.id)
        feedback = _feedback(session, [a.id for a in assessments])
        rows = [
            {"a": a, "trend": runs.trend(before.get(a.epic_key), a.rating), "before": before.get(a.epic_key),
             "feedback": feedback[a.id]}
            for a in assessments
        ]
        rows.sort(key=lambda r: (r["a"].detail.get("skipped") == "done", -report.RANK.get(r["a"].rating, 0),
                                 snapshot.natural_key(r["a"].epic_key)))
        return page(request, "overview.html", run=current, rows=rows, previous=previous, notice=notice,
                    all_runs=runs.list_runs(session, project), as_of_value=as_of or f"{current.as_of:%Y-%m-%d}")

    @app.post("/runs")
    def create_run(request: Request, session: SessionDep, as_of: Annotated[str, Form()]) -> Response:
        try:
            when = snapshot.parse_as_of(as_of)
        except ValueError as exc:
            raise HTTPException(400, f"bad date {as_of!r}") from exc
        try:
            result = assess.run(session, app.state.project, when, _llm(request, session), label="dashboard")
        except LLMAuthError as exc:
            session.rollback()
            return page(request, "error.html", status_code=502, message=str(exc),
                        hint="Check PEWA_ANTHROPIC_API_KEY / ANTHROPIC_API_KEY, or start the dashboard with --dry-run.")
        if not result.assessments:
            session.rollback()
            return page(request, "error.html", status_code=404,
                        message=f"No epics for project '{app.state.project}' as of {when:%Y-%m-%d}.",
                        hint="Run `pewa ingest` first.")
        return RedirectResponse(f"/?run={result.run_id}", status_code=303)

    @app.get("/assessments/{assessment_id}", response_class=HTMLResponse)
    def epic_detail(request: Request, session: SessionDep, assessment_id: int) -> Response:
        a = session.get(Assessment, assessment_id)
        if a is None:
            raise HTTPException(404, f"no assessment {assessment_id}")
        run = _run_or_404(session, app.state.project, a.run_id)

        # Rating history for this epic, oldest first: across live runs, or within one backtest.
        backtest_id = session.scalar(select(BacktestStep.backtest_id).where(BacktestStep.run_id == run.id))
        query = (select(Run.as_of, Assessment.rating, Assessment.id).join(Assessment, Assessment.run_id == Run.id)
                 .where(Run.project == run.project, Run.mode == run.mode, Assessment.epic_key == a.epic_key))
        if backtest_id is not None:
            query = query.join(BacktestStep, BacktestStep.run_id == Run.id).where(BacktestStep.backtest_id == backtest_id)
        history = session.execute(query.order_by(Run.as_of, Run.id)).all()

        # Signal timeline: every event a signal or a key risk points at, described as of the run.
        signal_ids: dict[str, list[str]] = {}
        for s in a.signals or []:
            for event_id in s.get("evidence", []):
                signal_ids.setdefault(event_id, []).append(s["type"])
        cited = {e["id"] for e in a.evidence or []}
        state = snapshot.build(session, run.project, run.as_of)
        by_id = {e.id: e for e in state.events}
        timeline = [
            {"event": by_id[i], "summary": assess.describe(by_id[i], state), "signals": sorted(set(signal_ids.get(i, []))),
             "cited": i in cited}
            for i in sorted(set(signal_ids) | cited, key=lambda i: by_id[i].occurred_at if i in by_id else run.as_of)
            if i in by_id
        ]
        evidence = {e["id"]: e for e in a.evidence or []}
        return page(request, "epic.html", a=a, run=run, history=history, timeline=timeline, evidence=evidence,
                    feedback=_feedback(session, [a.id])[a.id], backtest_id=backtest_id)

    @app.post("/assessments/{assessment_id}/feedback")
    def add_feedback(request: Request, session: SessionDep, assessment_id: int,
                     verdict: Annotated[str, Form()], note: Annotated[str, Form()] = "") -> Response:
        a = session.get(Assessment, assessment_id)
        if a is None:
            raise HTTPException(404, f"no assessment {assessment_id}")
        if verdict not in VERDICTS:
            raise HTTPException(400, f"verdict must be one of {', '.join(VERDICTS)}")
        session.add(Feedback(assessment_id=a.id, verdict=verdict, note=note.strip() or None,
                             created_at=datetime.now(UTC)))
        session.flush()
        if _is_htmx(request):
            return templates.TemplateResponse(request, "_feedback.html",
                                              {"a": a, "feedback": _feedback(session, [a.id])[a.id], "saved": True})
        return RedirectResponse(f"/assessments/{a.id}#feedback", status_code=303)

    @app.get("/events/{event_id}", response_class=HTMLResponse)
    def event_view(request: Request, session: SessionDep, event_id: str) -> Response:
        row = session.get(EventRow, event_id)
        if row is None or row.project != app.state.project:
            raise HTTPException(404, f"no event {event_id}")
        return page(request, "event.html", e=row, back=request.headers.get("referer"))

    @app.get("/reports", response_class=HTMLResponse)
    def reports_page(request: Request, session: SessionDep, run: int | None = None) -> Response:
        current = _run_or_404(session, app.state.project, run) if run else runs.latest_run(session, app.state.project)
        return page(request, "reports.html", run=current, all_runs=runs.list_runs(session, app.state.project),
                    reports=[])

    @app.post("/reports")
    def create_report(request: Request, session: SessionDep, run_id: Annotated[int, Form()],
                      variant: Annotated[str, Form()]) -> Response:
        current = _run_or_404(session, app.state.project, run_id)
        if variant not in report.VARIANTS:
            raise HTTPException(400, f"variant must be one of {', '.join(report.VARIANTS)}")
        try:
            draft = report.generate(session, current, _llm(request, session), variant)  # type: ignore[arg-type]
        except LLMAuthError as exc:
            return page(request, "error.html", status_code=502, message=str(exc),
                        hint="Check PEWA_ANTHROPIC_API_KEY / ANTHROPIC_API_KEY, or start the dashboard with --dry-run.")
        if _is_htmx(request):
            return templates.TemplateResponse(request, "_report.html", {"r": draft})
        return page(request, "reports.html", run=current, all_runs=runs.list_runs(session, app.state.project),
                    reports=[draft])

    @app.get("/backtests", response_class=HTMLResponse)
    def backtests_page(request: Request, session: SessionDep) -> Response:
        evs = [backtest.evaluate(session, bt) for bt in backtest.list_backtests(session, app.state.project)]
        return page(request, "backtests.html", evs=evs)

    @app.get("/backtests/compare", response_class=HTMLResponse)
    def compare_page(request: Request, session: SessionDep, a: int, b: int) -> Response:
        evs = [backtest.evaluate(session, _backtest_or_404(session, app.state.project, i)) for i in (a, b)]
        return page(request, "backtest_compare.html", a=evs[0], b=evs[1], evs=evs)

    @app.get("/backtests/{backtest_id}", response_class=HTMLResponse)
    def backtest_page(request: Request, session: SessionDep, backtest_id: int) -> Response:
        ev = backtest.evaluate(session, _backtest_or_404(session, app.state.project, backtest_id))
        return page(request, "backtest.html", ev=ev)

    @app.get("/healthz")
    def healthz(session: SessionDep) -> dict[str, Any]:
        return {"ok": True, "project": app.state.project,
                "runs": session.scalar(select(func.count(Run.id)).where(Run.project == app.state.project))}

    return app
