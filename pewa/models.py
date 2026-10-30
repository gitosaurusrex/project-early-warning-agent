"""SQLAlchemy tables."""

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """Store naive UTC in SQLite, hand back aware UTC datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are not allowed; pass an aware UTC datetime")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime, dict[str, Any]: JSON, list[Any]: JSON}


class EventRow(Base):
    """Append-only raw history. Never updated except for the derived epic_key."""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("project", "source", "kind", "entity_id", "occurred_at", name="uq_event_identity"),
        Index("ix_events_project_time", "project", "occurred_at"),
        Index("ix_events_epic", "project", "epic_key"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(48))
    occurred_at: Mapped[datetime]
    entity_id: Mapped[str] = mapped_column(String(255))
    # Resolved with everything known at ingest time; snapshots re-resolve links as of their own date.
    epic_key: Mapped[str | None] = mapped_column(String(64))
    actor: Mapped[str | None] = mapped_column(String(255))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    url: Mapped[str | None] = mapped_column(Text)


class EntityVersion(Base):
    """State of a ticket/epic/PR from valid_from until the next version. Derived from events."""

    __tablename__ = "entity_versions"
    __table_args__ = (
        UniqueConstraint("project", "entity_type", "entity_key", "valid_from", name="uq_entity_version"),
        Index("ix_entity_lookup", "project", "entity_type", "entity_key", "valid_from"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(16))  # ticket | pr
    entity_key: Mapped[str] = mapped_column(String(255))
    valid_from: Mapped[datetime]
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project: Mapped[str] = mapped_column(String(64))
    as_of: Mapped[datetime]
    mode: Mapped[str] = mapped_column(String(16))  # live | backtest
    model: Mapped[str] = mapped_column(String(64))
    label: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Assessment(Base):
    __tablename__ = "assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    epic_key: Mapped[str] = mapped_column(String(64))
    rating: Mapped[str] = mapped_column(String(16))  # green | amber | red | unknown
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    rationale: Mapped[str] = mapped_column(Text, default="")
    predicted_slip_days: Mapped[int | None] = mapped_column(Integer)
    evidence: Mapped[list[Any]] = mapped_column(default=list)
    signals: Mapped[list[Any]] = mapped_column(default=list)
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)  # key_risks, suggested_actions, errors


class Backtest(Base):
    """A replay over past dates (SPEC §12). Each step is an ordinary run with mode 'backtest'."""

    __tablename__ = "backtests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project: Mapped[str] = mapped_column(String(64))
    start: Mapped[date] = mapped_column(Date)
    end: Mapped[date] = mapped_column(Date)
    step_days: Mapped[int] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(String(64))
    label: Mapped[str | None] = mapped_column(String(128))
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class BacktestStep(Base):
    __tablename__ = "backtest_steps"
    __table_args__ = (UniqueConstraint("backtest_id", "run_id", name="uq_backtest_step"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    backtest_id: Mapped[int] = mapped_column(ForeignKey("backtests.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    as_of: Mapped[datetime]


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("assessments.id", ondelete="CASCADE"), index=True)
    verdict: Mapped[str] = mapped_column(String(16))  # useful | noise
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Outcome(Base):
    __tablename__ = "outcomes"
    __table_args__ = (UniqueConstraint("project", "epic_key", name="uq_outcome"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project: Mapped[str] = mapped_column(String(64))
    epic_key: Mapped[str] = mapped_column(String(64))
    planned_date: Mapped[date] = mapped_column(Date)
    actual_date: Mapped[date | None] = mapped_column(Date)
    slipped: Mapped[bool] = mapped_column(Boolean)


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    model: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(64))
    prompt_hash: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
