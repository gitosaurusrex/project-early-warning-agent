"""Connector protocol. Connectors are read-only and emit historical events, not just current state."""

from datetime import datetime
from typing import Protocol

from pewa.schemas import Event, Source


class ConnectorError(Exception):
    """A source could not be read (bad credentials, missing scope, unreachable, out of retries)."""


class Connector(Protocol):
    source: Source

    def fetch(self, since: datetime | None) -> list[Event]:
        """Events with occurred_at >= since (all history when since is None).

        `since` is inclusive: ingest de-duplicates, so re-fetching a boundary event is harmless
        and a strictly-greater filter could miss events sharing the last timestamp.
        """
        ...
