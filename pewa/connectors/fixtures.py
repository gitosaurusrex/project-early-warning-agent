"""Loads a bundled synthetic dataset (e.g. data/fixtures/atlas) as if it came from live sources."""

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pewa.config import get_settings
from pewa.schemas import Event, Source, to_utc


def fixture_dir(name: str, base: Path | None = None) -> Path:
    path = (base or get_settings().pewa_fixtures_dir) / name
    if not path.is_dir():
        raise FileNotFoundError(f"no fixture dataset named {name!r} at {path}")
    return path


class FixtureConnector:
    def __init__(self, name: str, source: Source, base: Path | None = None):
        self.name = name
        self.source = source
        self.path = fixture_dir(name, base) / f"{source}.json"

    def fetch(self, since: datetime | None = None) -> list[Event]:
        records = json.loads(self.path.read_text())
        events = [Event.model_validate({**record, "source": self.source}) for record in records]
        if since is not None:
            since = to_utc(since)
            events = [e for e in events if e.occurred_at >= since]
        return events


def load_outcomes(name: str, base: Path | None = None) -> list[dict[str, Any]]:
    records = json.loads((fixture_dir(name, base) / "outcomes.json").read_text())
    return [
        {
            "epic_key": r["epic_key"],
            "planned_date": date.fromisoformat(r["planned_date"]),
            "actual_date": date.fromisoformat(r["actual_date"]) if r.get("actual_date") else None,
            "slipped": bool(r["slipped"]),
        }
        for r in records
    ]


def load_meta(name: str, base: Path | None = None) -> dict[str, Any]:
    return json.loads((fixture_dir(name, base) / "meta.json").read_text())
