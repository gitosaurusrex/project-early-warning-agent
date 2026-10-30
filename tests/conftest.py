import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from pewa.config import Settings, get_settings
from pewa.connectors.fixtures import FixtureConnector
from pewa.db import make_engine, session_scope
from pewa.ingest import ingest
from pewa.schemas import SOURCES, Event


# Captured before the autouse fixture strips credentials; only the opt-in `llm` tests use it.
REAL_ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)  # type: ignore[misc]


def ev(source: str, kind: str, entity_id: str, when: datetime, actor: str | None = None, **payload: Any) -> Event:
    return Event(source=source, kind=kind, entity_id=entity_id, occurred_at=when, actor=actor, payload=payload)  # type: ignore[arg-type]


class ListConnector:
    """In-memory connector for hand-built event lists."""

    def __init__(self, source: str, events: list[Event]):
        self.source = source
        self.events = [e for e in events if e.source == source]

    def fetch(self, since: datetime | None) -> list[Event]:
        return [e for e in self.events if since is None or e.occurred_at >= since]


def ingest_events(session: Session, project: str, events: list[Event]) -> None:
    ingest(session, project, [ListConnector(s, events) for s in SOURCES], incremental=False)


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep tests independent of any developer .env or credentials."""
    for var in ("JIRA_API_TOKEN", "GITHUB_TOKEN", "SLACK_BOT_TOKEN", "ANTHROPIC_API_KEY", "PEWA_PROJECT", "PEWA_DB_PATH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return make_engine(tmp_path / "test.db")


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with session_scope(engine) as s:
        yield s


@pytest.fixture(scope="session")
def atlas_engine(tmp_path_factory: pytest.TempPathFactory) -> Engine:
    """A database with the full Atlas fixture set ingested once per test session."""
    engine = make_engine(tmp_path_factory.mktemp("atlas") / "atlas.db")
    with session_scope(engine) as s:
        ingest(s, "atlas", [FixtureConnector("atlas", src) for src in SOURCES])
    return engine


@pytest.fixture
def atlas(atlas_engine: Engine) -> Iterator[Session]:
    with session_scope(atlas_engine) as s:
        yield s
        s.rollback()
