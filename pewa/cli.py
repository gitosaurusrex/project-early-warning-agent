"""`pewa` command-line interface."""

import logging
import os
import shutil
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from pewa import assess as assess_mod
from pewa import report as report_mod
from pewa import runs as runs_mod
from pewa import snapshot as snapshot_mod
from pewa.config import REPO_ROOT, get_settings
from pewa.connectors.base import Connector
from pewa.connectors.fixtures import FixtureConnector, fixture_dir, load_outcomes
from pewa.db import make_engine, session_scope
from pewa.ingest import ingest as run_ingest
from pewa.ingest import save_outcomes
from pewa.llm import LLM, LLMAuthError
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
) -> None:
    """Fetch events from sources (or fixtures) and append them to the store."""
    settings = get_settings()
    sources = list(SOURCES) if source is SourceOption.all else [source.value]

    if fixtures is None:
        live = [s for s in sources if settings.has_credentials(s)]
        for s in sorted(set(sources) - set(live)):
            err.print(f"[yellow]warning:[/yellow] skipping {s}: credentials not configured")
        if live:
            err.print("[red]Live connectors are not implemented yet (Phase 5). Use --fixtures atlas.[/red]")
            raise typer.Exit(1)
        err.print(f"No live credentials configured, using the bundled '{DEFAULT_FIXTURES}' fixtures.")
        fixtures = DEFAULT_FIXTURES

    try:
        fixture_dir(fixtures)
    except FileNotFoundError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    project = fixtures
    connectors: list[Connector] = [FixtureConnector(fixtures, s) for s in sources]  # type: ignore[arg-type]

    engine = make_engine(settings.pewa_db_path)
    with session_scope(engine) as session:
        result = run_ingest(session, project, connectors, incremental=not full)
        outcomes = save_outcomes(session, project, load_outcomes(fixtures))

    table = Table(title=f"Ingested project {project!r}")
    table.add_column("source")
    table.add_column("fetched", justify="right")
    table.add_column("new", justify="right")
    for s in sources:
        table.add_row(s, str(result.fetched.get(s, 0)), str(result.inserted.get(s, 0)))
    console.print(table)
    console.print(f"database: {settings.pewa_db_path}")
    console.print(f"entity versions written: {result.entity_versions}, event links updated: {result.linked_events}, outcomes: {outcomes}")


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
