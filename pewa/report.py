"""Weekly status reports from a stored run (SPEC §10.2), with "no new facts" checks enforced in code.

The model writes the Markdown. Its draft must stay within the word limit, mention only ticket
keys, evidence ids and URLs that appear in its input, and (engineering variant) cover every
epic. An invalid draft is retried once with the problem; a second failure falls back to the
deterministic template, which is also what dry-run mode returns.
"""

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from pewa import runs
from pewa.llm import LLM, LLMAuthError, LLMError
from pewa.models import Run
from pewa.snapshot import natural_key

log = logging.getLogger(__name__)

PROMPTS = Path(__file__).parent / "prompts"
TEMPLATES = Path(__file__).parent / "report_templates"
WORD_LIMITS = {"exec": 200, "eng": 600}
# A run counts as "last week" when it is at least this much older than the report's run.
WEEK_GAP = timedelta(days=6)
RANK = {"red": 3, "amber": 2, "unknown": 1, "green": 0}

Variant = Literal["exec", "eng"]
VARIANTS: tuple[Variant, ...] = ("exec", "eng")


class EvidenceLink(BaseModel):
    id: str
    url: str | None = None
    when: str = ""
    summary: str = ""


class RiskSummary(BaseModel):
    summary: str
    evidence: list[EvidenceLink] = Field(default_factory=list)


class EpicSummary(BaseModel):
    key: str
    title: str
    owner: str | None = None
    due_date: str | None = None
    rating: str
    previous_rating: str | None = None
    trend: str = "new"
    confidence: float = 0.0
    rationale: str = ""
    predicted_slip_days: int | None = None
    key_risks: list[RiskSummary] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
    done: bool = False


class ReportInput(BaseModel):
    """Everything a report may say, taken from one run's assessments (plus last week's ratings)."""

    project: str
    run_id: int
    as_of: datetime
    model: str
    previous_as_of: datetime | None = None
    overall: str
    epics: list[EpicSummary]

    @property
    def open_epics(self) -> list[EpicSummary]:
        return [e for e in self.epics if not e.done]

    @property
    def done_epics(self) -> list[EpicSummary]:
        return [e for e in self.epics if e.done]

    @property
    def changes(self) -> list[EpicSummary]:
        return [e for e in self.epics if e.trend in ("worse", "better")]

    def counts(self) -> dict[str, int]:
        return {r: sum(1 for e in self.epics if e.rating == r) for r in ("red", "amber", "green", "unknown")}

    def top_risks(self, n: int) -> list[tuple[EpicSummary, RiskSummary]]:
        """Round-robin over flagged epics (worst first), so one epic can't fill the list."""
        flagged = [e for e in self.open_epics if e.rating in ("red", "amber")]
        picked: list[tuple[EpicSummary, RiskSummary]] = []
        for depth in range(max((len(e.key_risks) for e in flagged), default=0)):
            picked += [(e, e.key_risks[depth]) for e in flagged if depth < len(e.key_risks)]
        return picked[:n]

    def decisions(self, n: int) -> list[tuple[EpicSummary, str]]:
        flagged = [e for e in self.open_epics if e.rating in ("red", "amber") and e.suggested_actions]
        return [(e, e.suggested_actions[0]) for e in flagged][:n]


class Report(BaseModel):
    variant: Variant
    markdown: str
    run_id: int
    as_of: datetime
    source: Literal["model", "template"]
    words: int
    attempts: int = 0
    error: str | None = None  # why the model's draft was replaced by the template


class InvalidReport(ValueError):
    pass


# ----- input ------------------------------------------------------------------------------------


def gather(session: Session, run: Run) -> ReportInput:
    """Turn a stored run (and the run about a week before it) into report input."""
    previous = runs.previous_run(session, run, min_gap=WEEK_GAP)
    before = {a.epic_key: a.rating for a in runs.assessments(session, previous.id)} if previous else {}
    epics = []
    for a in runs.assessments(session, run.id):
        detail = a.detail or {}
        by_id = {e["id"]: e for e in a.evidence or []}
        epics.append(EpicSummary(
            key=a.epic_key,
            title=detail.get("title") or a.epic_key,
            owner=detail.get("owner"),
            due_date=detail.get("due_date"),
            rating=a.rating,
            previous_rating=before.get(a.epic_key),
            trend=runs.trend(before.get(a.epic_key), a.rating),
            confidence=a.confidence,
            rationale=a.rationale,
            predicted_slip_days=a.predicted_slip_days,
            key_risks=[
                RiskSummary(summary=r["summary"],
                            evidence=[EvidenceLink(**{k: v for k, v in by_id[i].items() if k in EvidenceLink.model_fields})
                                      if i in by_id else EvidenceLink(id=i) for i in r.get("evidence_ids", [])])
                for r in detail.get("key_risks", [])
            ],
            suggested_actions=detail.get("suggested_actions") or [],
            done=detail.get("skipped") == "done",
        ))
    epics.sort(key=lambda e: (e.done, -RANK.get(e.rating, 0), natural_key(e.key)))
    rated = [e.rating for e in epics if e.rating in runs.RATING_RANK]
    overall = max(rated, key=runs.RATING_RANK.__getitem__) if rated else "unknown"
    return ReportInput(project=run.project, run_id=run.id, as_of=run.as_of, model=run.model,
                       previous_as_of=previous.as_of if previous else None, overall=overall, epics=epics)


# ----- rendering --------------------------------------------------------------------------------

_prompt_env = Environment(loader=FileSystemLoader(PROMPTS), undefined=StrictUndefined, keep_trailing_newline=True,
                          trim_blocks=True, lstrip_blocks=True)
_template_env = Environment(loader=FileSystemLoader(TEMPLATES), undefined=StrictUndefined,
                            trim_blocks=True, lstrip_blocks=True)


def word_count(markdown: str) -> int:
    """Words a reader sees: link targets and Markdown punctuation don't count."""
    text = re.sub(r"\]\([^)]*\)", "]", markdown)
    text = re.sub(r"https?://\S+", "", text)
    return sum(1 for token in text.split() if re.search(r"[A-Za-z0-9]", token))


def render_template(data: ReportInput, variant: Variant) -> str:
    """Deterministic report: the most detail that fits the word limit."""
    template = _template_env.get_template(f"{variant}.md.j2")
    markdown = ""
    for n in (3, 2, 1, 0):
        markdown = template.render(data=data, n=n).strip() + "\n"
        if word_count(markdown) <= WORD_LIMITS[variant]:
            break
    return markdown


def render_prompt(data: ReportInput, variant: Variant) -> tuple[str, str]:
    system = (PROMPTS / "report_system.md").read_text()
    user = _prompt_env.get_template("report_user.md.j2").render(
        data=data, variant=variant, limit=WORD_LIMITS[variant],
        as_of=f"{data.as_of:%Y-%m-%d}",
        previous_as_of=f"{data.previous_as_of:%Y-%m-%d}" if data.previous_as_of else None,
    )
    return system, user


# ----- validation -------------------------------------------------------------------------------

KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
EVT_RE = re.compile(r"\bevt_[0-9a-f]{6,}\b")
URL_RE = re.compile(r"https?://[^\s)\]>\"']+")


def validate(text: str, source_text: str, data: ReportInput, variant: Variant) -> str:
    """Return the cleaned Markdown, or raise InvalidReport saying what to fix."""
    markdown = re.sub(r"^```(?:markdown|md)?\s*\n|\n?```\s*$", "", text.strip()).strip()
    if not markdown:
        raise InvalidReport("the report is empty")
    problems = []
    words = word_count(markdown)
    if words > WORD_LIMITS[variant]:
        problems.append(f"it is {words} words; the limit is {WORD_LIMITS[variant]}")
    for label, pattern in (("ticket keys", KEY_RE), ("evidence ids", EVT_RE), ("URLs", URL_RE)):
        unknown = sorted(set(pattern.findall(markdown)) - set(pattern.findall(source_text)))
        if unknown:
            problems.append(f"it mentions {label} that are not in the input: {', '.join(unknown)}")
    if variant == "eng":
        missing = [e.key for e in data.epics if not re.search(rf"\b{re.escape(e.key)}\b", markdown)]
        if missing:
            problems.append(f"it leaves out epics: {', '.join(missing)}")
    if problems:
        raise InvalidReport("; ".join(problems))
    return markdown + "\n"


# ----- generating -------------------------------------------------------------------------------


def generate(session: Session, run: Run, llm: LLM, variant: Variant) -> Report:
    data = gather(session, run)
    fallback = render_template(data, variant)

    def template_report(attempts: int, error: str | None) -> Report:
        return Report(variant=variant, markdown=fallback, run_id=run.id, as_of=run.as_of, source="template",
                      words=word_count(fallback), attempts=attempts, error=error)

    system, user = render_prompt(data, variant)
    messages: list[dict] = [{"role": "user", "content": user}]
    error = None
    for attempt in (1, 2):
        try:
            text = llm.complete(f"report_{variant}", system, messages, canned=lambda: fallback)
        except LLMAuthError:
            raise
        except LLMError as exc:
            return template_report(attempt, str(exc))
        try:
            markdown = validate(text, system + user, data, variant)
        except InvalidReport as exc:
            error = str(exc)
            log.warning("%s report invalid (attempt %d): %s", variant, attempt, error)
            messages += [
                {"role": "assistant", "content": text or "(empty)"},
                {"role": "user", "content": f"That report can't be used: {error}. "
                                            "Reply with the corrected Markdown report only."},
            ]
            continue
        return Report(variant=variant, markdown=markdown, run_id=run.id, as_of=run.as_of,
                      source="template" if llm.dry_run else "model", words=word_count(markdown), attempts=attempt)
    return template_report(2, error)
