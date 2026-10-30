"""`pewa` command-line interface."""

import json
import logging
import os
import shutil
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path

import typer
from sqlalchemy import select
from rich.console import Console
from rich.table import Table

from pewa import assess as assess_mod
from pewa import backtest as backtest_mod
from pewa import report as report_mod
from pewa import runs as runs_mod
from pewa import snapshot as snapshot_mod
from pewa.config import REPO_ROOT, get_settings
from pewa.connectors.base import Connector
from pewa.connectors.fixtures import FixtureConnector, fixture_dir, load_outcomes, parse_outcomes
from pewa.connectors.live import make_connector
from pewa.db import make_engine, session_scope
from pewa.ingest import ingest as run_ingest
from pewa.ingest import derive_outcomes, save_outcomes
from pewa.llm import LLM, LLMAuthError
from pewa.models import Outcome
from pewa.schemas import SOURCES

app = typer.Typer(help="Project Early-Warning Agent", no_args_is_help=True, add_completion=False)
console = Console()
err = Console(stderr=True, soft_wrap=True)

DEFAULT_FIXTURES = "atlas"


class SourceOption(StrEnum):
    all = "all"
    jira = "jira"
    github = "github"
    slack = "slack"


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v", help="Log progress at INFO level.")) -> None:
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


@app.command()
def init() -> None:
    """Create the database and write .env from .env.example (if .env does not exist)."""
    settings = get_settings()
    make_engine(settings.pewa_db_path)
    console.print(f"Database ready at [bold]{settings.pewa_db_path}[/bold]")
    env, example = Path(".env"), REPO_ROOT / ".env.example"
    if env.exists():
        console.print(".env already exists, left unchanged")
    elif example.exists():
        shutil.copy(example, env)
        console.print("Wrote .env from .env.example. Fill in credentials to use live sources.")


@app.command()
def ingest(
    source: SourceOption = typer.Option(SourceOption.all, help="Which source to ingest."),
    fixtures: str | None = typer.Option(None, help="Load a bundled synthetic dataset (e.g. 'atlas') instead of live sources."),
    full: bool = typer.Option(False, help="Re-fetch all history instead of syncing from the last ingested event."),
    project: str | None = typer.Option(None, help="Project name for live data (defaults to PEWA_PROJECT)."),
) -> None:
    """Fetch events from sources (or fixtures) and append them to the store."""
    settings = get_settings()
    sources = list(SOURCES) if source is SourceOption.all else [source.value]
    connectors: list[Connector]

    if fixtures is None:
        live = [s for s in sources if settings.has_credentials(s)]
        for s in sorted(set(sources) - set(live)):
            err.print(f"[yellow]warning:[/yellow] skipping {s}: credentials not configured")
        if not live:
            err.print(f"No live credentials configured, using the bundled '{DEFAULT_FIXTURES}' fixtures.")
            fixtures = DEFAULT_FIXTURES

    if fixtures is None:
        project = project or settings.pewa_project
        if (settings.pewa_fixtures_dir / project).is_dir():
            err.print(f"[red]Project '{project}' is the name of a bundled fixture dataset.[/red] Set PEWA_PROJECT "
                      "(or --project) to a name for your live project so the two don't mix.")
            raise typer.Exit(1)
        try:
            connectors = [make_connector(s, settings) for s in live]
        except ValueError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        outcome_rows: list = []
    else:
        try:
            fixture_dir(fixtures)
        except FileNotFoundError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        project = fixtures
        connectors = [FixtureConnector(fixtures, s) for s in sources]  # type: ignore[arg-type]
        outcome_rows = load_outcomes(fixtures)
    sources = [c.source for c in connectors]

    engine = make_engine(settings.pewa_db_path)
    with session_scope(engine) as session:
        result = run_ingest(session, project, connectors, incremental=not full)
        outcomes = save_outcomes(session, project, outcome_rows)

    table = Table(title=f"Ingested project {project!r}")
    table.add_column("source")
    table.add_column("fetched", justify="right")
    table.add_column("new", justify="right")
    for s in sources:
        table.add_row(s, str(result.fetched.get(s, 0)), str(result.inserted.get(s, 0)))
    console.print(table)
    console.print(f"database: {settings.pewa_db_path}")
    console.print(f"entity versions written: {result.entity_versions}, event links updated: {result.linked_events}, outcomes: {outcomes}")
    for s, message in result.errors.items():
        err.print(f"[red]error:[/red] {s} was skipped: {message}")
    if result.errors:
        raise typer.Exit(1)


@app.command()
def outcomes(
    file: Path | None = typer.Option(None, "--file", help="Load ground truth from a JSON file (same format as "
                                     "data/fixtures/atlas/outcomes.json)."),
    derive: bool = typer.Option(False, "--derive", help="Derive it from finished epics: planned = first due date "
                                "set, actual = when the epic reached Done."),
    project: str | None = typer.Option(None, help="Project name (defaults to PEWA_PROJECT)."),
) -> None:
    """Show, load or derive the ground truth that backtests are scored against."""
    settings = get_settings()
    project = project or settings.pewa_project
    if file and derive:
        raise typer.BadParameter("use either --file or --derive", param_hint="--file")
    with session_scope(make_engine(settings.pewa_db_path)) as session:
        if file or derive:
            try:
                rows = parse_outcomes(json.loads(file.read_text())) if file else derive_outcomes(session, project)
            except (OSError, ValueError, KeyError) as exc:
                err.print(f"[red]Could not read outcomes: {exc}[/red]")
                raise typer.Exit(1) from exc
            err.print(f"Saved {save_outcomes(session, project, rows)} outcome(s) for '{project}'.")
        stored = session.scalars(select(Outcome).where(Outcome.project == project).order_by(Outcome.epic_key)).all()
    if not stored:
        err.print(f"No outcomes for '{project}'. Load them with --file, or --derive them from finished epics.")
        raise typer.Exit(1)
    table = Table(title=f"Outcomes for {project}")
    for col in ("epic", "planned", "delivered", "slipped"):
        table.add_column(col)
    for o in stored:
        table.add_row(o.epic_key, str(o.planned_date), str(o.actual_date or "-"), "yes" if o.slipped else "no")
    console.print(table)


@app.command()
def snapshot(
    as_of: str = typer.Option(..., "--as-of", help="Date (end of that day, UTC) or ISO datetime."),
    project: str | None = typer.Option(None, help="Project name (defaults to PEWA_PROJECT)."),
) -> None:
    """Show what the store knew about each epic as of a past moment."""
    settings = get_settings()
    project = project or settings.pewa_project
    when = snapshot_mod.parse_as_of(as_of)
    with session_scope(make_engine(settings.pewa_db_path)) as session:
        state = snapshot_mod.build(session, project, when)

    if not state.events:
        err.print(f"No events for project '{project}' on or before {when:%Y-%m-%d %H:%M} UTC. Run `pewa ingest` first?")
        raise typer.Exit(1)
    table = Table(title=f"{project} as of {when:%Y-%m-%d %H:%M} UTC ({len(state.events)} events)")
    for col in ("epic", "title", "status", "owner", "due", "tickets done", "open PRs", "events"):
        table.add_column(col)
    for epic in state.epics.values():
        children = state.epic_children(epic.key)
        done = sum(1 for t in children if t.category == "done")
        open_prs = sum(1 for pr in state.epic_prs(epic.key) if pr.state == "open")
        due = epic.due_date or "-"
        if epic.original_due_date and epic.original_due_date != epic.due_date:
            due = f"{due} (was {epic.original_due_date})"
        table.add_row(epic.key, epic.title, epic.status, epic.owner or "-", due, f"{done}/{len(children)}",
                      str(open_prs), str(len(state.epic_events(epic.key))))
    console.print(table)


def _resolve_dry_run(dry_run: bool) -> bool:
    """Fall back to dry-run (with a warning) when no API key is configured."""
    settings = get_settings()
    if not dry_run and not (settings.anthropic_key() or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        err.print("[yellow]warning:[/yellow] no Anthropic API key (PEWA_ANTHROPIC_API_KEY or ANTHROPIC_API_KEY),"
                  " running with --dry-run (offline heuristic).")
        return True
    return dry_run


RATING_STYLE = {"green": "green", "amber": "yellow", "red": "red", "unknown": "magenta"}


@app.command()
def assess(
    as_of: str | None = typer.Option(None, "--as-of", help="Date (end of that day, UTC) or ISO datetime. Default: now."),
    project: str | None = typer.Option(None, help="Project name (defaults to PEWA_PROJECT)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Don't call the model; use the offline heuristic responder."),
    label: str | None = typer.Option(None, help="Free-text label stored on the run (e.g. a prompt version)."),
) -> None:
    """Run signals and the LLM risk assessment for every epic, store the run and print it."""
    settings = get_settings()
    project = project or settings.pewa_project
    when = snapshot_mod.parse_as_of(as_of) if as_of else datetime.now(UTC)
    dry_run = _resolve_dry_run(dry_run)

    try:
        with session_scope(make_engine(settings.pewa_db_path)) as session:
            llm = LLM(settings.pewa_model, dry_run=dry_run, session=session)
            result = assess_mod.run(session, project, when, llm, label=label)
    except LLMAuthError as exc:
        err.print(f"[red]{exc}[/red]\nCheck PEWA_ANTHROPIC_API_KEY / ANTHROPIC_API_KEY, or run with --dry-run.")
        raise typer.Exit(1) from exc

    if not result.assessments:
        err.print(f"No epics for project '{project}' as of {when:%Y-%m-%d}. Run `pewa ingest` first?")
        raise typer.Exit(1)
    table = Table(title=f"{project} as of {result.as_of:%Y-%m-%d %H:%M} UTC · run {result.run_id} · {result.model}")
    for col in ("epic", "title", "rating", "conf", "rationale", "signals", "cites"):
        table.add_column(col)
    for a in result.assessments:
        style = RATING_STYLE[a.rating]
        table.add_row(a.epic_key, a.title, f"[{style}]{a.rating}[/{style}]", f"{a.confidence:.2f}",
                      a.rationale if a.rating != "unknown" else f"error: {a.error}",
                      str(len(a.signals)), str(len(a.evidence)))
    console.print(table)
    for a in result.assessments:
        for risk in a.key_risks:
            console.print(f"[bold]{a.epic_key}[/bold] · {risk.summary} [dim]({', '.join(risk.evidence_ids)})[/dim]")


class VariantOption(StrEnum):
    exec = "exec"
    eng = "eng"
    both = "both"


@app.command()
def report(
    as_of: str | None = typer.Option(None, "--as-of", help="Use the latest run on or before this date. Default: latest run."),
    variant: VariantOption = typer.Option(VariantOption.both, help="Exec summary, engineering detail, or both."),
    out: Path | None = typer.Option(None, "--out", help="Write the Markdown to this file instead of printing it."),
    project: str | None = typer.Option(None, help="Project name (defaults to PEWA_PROJECT)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Don't call the model; fill the deterministic template."),
) -> None:
    """Draft weekly status updates (Markdown) from a stored assessment run."""
    settings = get_settings()
    project = project or settings.pewa_project
    when = snapshot_mod.parse_as_of(as_of) if as_of else None
    dry_run = _resolve_dry_run(dry_run)
    variants = list(report_mod.VARIANTS) if variant is VariantOption.both else [variant.value]

    try:
        with session_scope(make_engine(settings.pewa_db_path)) as session:
            run = runs_mod.latest_run(session, project, when)
            if run is None:
                on = f" on or before {when:%Y-%m-%d}" if when else ""
                err.print(f"No assessment run for '{project}'{on}. Run `pewa assess{f' --as-of {as_of}' if as_of else ''}` first.")
                raise typer.Exit(1)
            llm = LLM(settings.pewa_model, dry_run=dry_run, session=session)
            reports = [report_mod.generate(session, run, llm, v) for v in variants]  # type: ignore[arg-type]
    except LLMAuthError as exc:
        err.print(f"[red]{exc}[/red]\nCheck PEWA_ANTHROPIC_API_KEY / ANTHROPIC_API_KEY, or run with --dry-run.")
        raise typer.Exit(1) from exc

    for r in reports:
        how = "template" if r.source == "template" else f"model, {r.attempts} attempt(s)"
        err.print(f"{r.variant}: run {r.run_id} as of {r.as_of:%Y-%m-%d %H:%M} UTC, {r.words} words ({how})")
        if r.error:
            err.print(f"[yellow]warning:[/yellow] {r.variant} draft fell back to the template: {r.error}")
    markdown = "\n---\n\n".join(r.markdown for r in reports)
    if out:
        out.write_text(markdown)
        err.print(f"Wrote {out}")
    else:
        print(markdown, end="")


DEFAULT_REPORT_DIR = Path("backtest_reports")


def _parse_date(value: str, option: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"expected YYYY-MM-DD, got {value!r}", param_hint=option) from exc


def _print_metrics(ev: backtest_mod.Evaluation) -> None:
    table = Table(title=f"{ev.name} · {ev.project} · {ev.model}")
    table.add_column("metric")
    table.add_column("value")
    for name, value, _how in ev.metric_rows():
        table.add_row(name, value)
    console.print(table)
    epics = Table()
    for col in ("epic", "outcome", "first amber/red before due", "verdict"):
        epics.add_column(col)
    for e in ev.epics:
        epics.add_row(f"{e.key} {e.title}", e.outcome, f"{e.first_flag:%d %b}" if e.first_flag else "-", e.verdict)
    console.print(epics)


@app.command()
def backtest(
    start: str | None = typer.Option(None, "--from", help="First step date (YYYY-MM-DD). Default: first event."),
    end: str | None = typer.Option(None, "--to", help="Last possible step date (YYYY-MM-DD). Default: last event."),
    step: str = typer.Option("7d", help="Step between replays, e.g. 7d or 1w."),
    project: str | None = typer.Option(None, help="Project name (defaults to PEWA_PROJECT)."),
    model: str | None = typer.Option(None, help="Model to assess with (defaults to PEWA_MODEL)."),
    label: str | None = typer.Option(None, help="Free-text label, e.g. the prompt version being compared."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Don't call the model; use the offline heuristic responder."),
    out: Path = typer.Option(DEFAULT_REPORT_DIR, "--out", help="Directory for the Markdown and HTML reports."),
) -> None:
    """Replay the project week by week, score the ratings against outcomes, and write a report."""
    settings = get_settings()
    project = project or settings.pewa_project
    try:
        step_days = backtest_mod.parse_step(step)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--step") from exc
    dry_run = _resolve_dry_run(dry_run)

    with session_scope(make_engine(settings.pewa_db_path)) as session:
        span = backtest_mod.event_range(session, project)
        if span is None:
            err.print(f"No events for project '{project}'. Run `pewa ingest` first?")
            raise typer.Exit(1)
        first = _parse_date(start, "--from") if start else span[0]
        last = _parse_date(end, "--to") if end else span[1]
        try:
            dates = backtest_mod.step_dates(first, last, step_days)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--to") from exc
        llm = LLM(model or settings.pewa_model, dry_run=dry_run, session=session)
        err.print(f"Replaying {project} at {len(dates)} steps from {first} to {dates[-1]} with {llm.label}...")

        def progress(day: date, result: assess_mod.RunResult) -> None:
            ratings = ", ".join(f"{a.epic_key} {a.rating}" for a in result.assessments)
            err.print(f"  {day:%a %d %b}: {ratings}")
            session.commit()  # keep finished steps if a later one fails

        try:
            bt = backtest_mod.run(session, project, first, last, step_days, llm, label=label, on_step=progress)
        except LLMAuthError as exc:
            err.print(f"[red]{exc}[/red]\nCheck PEWA_ANTHROPIC_API_KEY / ANTHROPIC_API_KEY, or run with --dry-run.")
            raise typer.Exit(1) from exc
        ev = backtest_mod.evaluate(session, bt)

    _print_metrics(ev)
    paths = backtest_mod.write_reports(out, f"backtest-{ev.backtest_id}", backtest_mod.render(ev, "md"),
                                       backtest_mod.render(ev, "html"))
    console.print("Reports: " + ", ".join(str(p) for p in paths))


@app.command()
def backtests(
    backtest_id: int | None = typer.Argument(None, help="Show this backtest's metrics and rewrite its reports."),
    project: str | None = typer.Option(None, help="Project name (defaults to PEWA_PROJECT)."),
    out: Path = typer.Option(DEFAULT_REPORT_DIR, "--out", help="Directory for the Markdown and HTML reports."),
) -> None:
    """List stored backtests, or re-score one."""
    settings = get_settings()
    project = project or settings.pewa_project
    with session_scope(make_engine(settings.pewa_db_path)) as session:
        if backtest_id is None:
            rows = [backtest_mod.evaluate(session, bt) for bt in backtest_mod.list_backtests(session, project)]
        else:
            bt = session.get(backtest_mod.Backtest, backtest_id)
            if bt is None or bt.project != project:
                err.print(f"No backtest {backtest_id} for project '{project}'.")
                raise typer.Exit(1)
            ev = backtest_mod.evaluate(session, bt)
    if backtest_id is not None:
        _print_metrics(ev)
        paths = backtest_mod.write_reports(out, f"backtest-{ev.backtest_id}", backtest_mod.render(ev, "md"),
                                           backtest_mod.render(ev, "html"))
        console.print("Reports: " + ", ".join(str(p) for p in paths))
        return
    if not rows:
        err.print(f"No backtests for '{project}' yet. Try `pewa backtest --dry-run`.")
        raise typer.Exit(1)
    table = Table(title=f"Backtests for {project}")
    for col in ("id", "label", "model", "range", "steps", "recall", "precision", "lead time", "cost"):
        table.add_column(col)
    for ev in rows:
        values = dict((name, value) for name, value, _ in ev.metric_rows())
        table.add_row(str(ev.backtest_id), ev.label or "-", ev.model, f"{ev.start} to {ev.end}", str(len(ev.steps)),
                      values["Recall"], values["Precision"], values["Lead time"], f"${ev.cost_usd:.2f}")
    console.print(table)


@app.command()
def compare(
    first: int = typer.Argument(..., help="Backtest id."),
    second: int = typer.Argument(..., help="Backtest id to compare against."),
    project: str | None = typer.Option(None, help="Project name (defaults to PEWA_PROJECT)."),
    out: Path = typer.Option(DEFAULT_REPORT_DIR, "--out", help="Directory for the Markdown and HTML reports."),
) -> None:
    """Compare two backtests side by side (e.g. two prompts or models)."""
    settings = get_settings()
    project = project or settings.pewa_project
    with session_scope(make_engine(settings.pewa_db_path)) as session:
        evs = []
        for backtest_id in (first, second):
            bt = session.get(backtest_mod.Backtest, backtest_id)
            if bt is None or bt.project != project:
                err.print(f"No backtest {backtest_id} for project '{project}'.")
                raise typer.Exit(1)
            evs.append(backtest_mod.evaluate(session, bt))
    a, b = evs
    table = Table(title=f"{a.name} vs {b.name}")
    for col in ("metric", f"{a.name} · {a.model}", f"{b.name} · {b.model}"):
        table.add_column(col)
    for ra, rb in zip(a.metric_rows(), b.metric_rows()):
        table.add_row(ra[0], ra[1], rb[1])
    console.print(table)
    paths = backtest_mod.write_reports(out, f"compare-{first}-{second}", backtest_mod.render_compare(a, b, "md"),
                                       backtest_mod.render_compare(a, b, "html"))
    console.print("Reports: " + ", ".join(str(p) for p in paths))


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Interface to bind."),
    port: int = typer.Option(8000, help="Port to listen on."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Never call the model from the dashboard."),
) -> None:
    """Start the local dashboard."""
    import uvicorn

    from pewa.web.app import create_app

    settings = get_settings()
    dry_run = _resolve_dry_run(dry_run)
    console.print(f"Dashboard for '{settings.pewa_project}' at http://{host}:{port} (database {settings.pewa_db_path})")
    uvicorn.run(create_app(dry_run=dry_run), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    app()
