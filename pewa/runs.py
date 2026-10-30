"""Looking up stored runs and their assessments (shared by reports and the dashboard)."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from pewa.models import Assessment, Run

RATING_RANK = {"green": 0, "amber": 1, "red": 2}


def latest_run(session: Session, project: str, as_of: datetime | None = None, mode: str = "live") -> Run | None:
    """The newest run whose as_of is on or before `as_of` (any as_of when None)."""
    query = select(Run).where(Run.project == project, Run.mode == mode)
    if as_of is not None:
        query = query.where(Run.as_of <= as_of)
    return session.scalars(query.order_by(Run.as_of.desc(), Run.id.desc()).limit(1)).first()


def previous_run(session: Session, run: Run, min_gap: timedelta = timedelta(0)) -> Run | None:
    """The newest comparable run at least `min_gap` before `run` (strictly earlier).

    Comparable means same project, mode and model: a rating that changed because the model (or
    dry-run) changed is not a trend."""
    query = select(Run).where(Run.project == run.project, Run.mode == run.mode, Run.model == run.model,
                              Run.id != run.id, Run.as_of < run.as_of, Run.as_of <= run.as_of - min_gap)
    return session.scalars(query.order_by(Run.as_of.desc(), Run.id.desc()).limit(1)).first()


def list_runs(session: Session, project: str, mode: str = "live", limit: int = 50) -> list[Run]:
    query = select(Run).where(Run.project == project, Run.mode == mode)
    return list(session.scalars(query.order_by(Run.as_of.desc(), Run.id.desc()).limit(limit)))


def assessments(session: Session, run_id: int) -> list[Assessment]:
    rows = session.scalars(select(Assessment).where(Assessment.run_id == run_id).order_by(Assessment.id))
    return list(rows)


def trend(before: str | None, after: str) -> str:
    """'worse', 'better', 'same', or 'new' (no comparable earlier rating)."""
    if before not in RATING_RANK or after not in RATING_RANK:
        return "new"
    diff = RATING_RANK[after] - RATING_RANK[before]
    return "worse" if diff > 0 else "better" if diff < 0 else "same"
