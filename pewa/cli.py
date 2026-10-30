"""`pewa` command-line interface."""

import logging
import shutil
from enum import StrEnum
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from pewa import snapshot as snapshot_mod
from pewa.config import REPO_ROOT, get_settings
from pewa.connectors.base import Connector
from pewa.connectors.fixtures import FixtureConnector, fixture_dir, load_outcomes
from pewa.db import make_engine, session_scope
from pewa.ingest import ingest as run_ingest
from pewa.ingest import save_outcomes
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


if __name__ == "__main__":
    app()
