"""SPEC §10.2 / §15: status reports, their "no new facts" checks, and the end-to-end CLI flow."""

import pytest
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from pewa import assess, report, runs, snapshot
from pewa.cli import app
from pewa.llm import LLM, LLMError
from pewa.models import Run

WEEK5, WEEK6 = "2026-02-06", "2026-02-13"


def dry() -> LLM:
    return LLM("test-model", dry_run=True)


def assess_at(session: Session, as_of: str) -> Run:
    result = assess.run(session, "atlas", snapshot.parse_as_of(as_of), dry())
    return session.get(Run, result.run_id)


@pytest.fixture
def two_weeks(atlas: Session) -> tuple[Run, Run]:
    return assess_at(atlas, WEEK5), assess_at(atlas, WEEK6)


class FailingLLM(LLM):
    def complete(self, *args, **kwargs):
        raise LLMError("API error 529: overloaded")


# ----- input and templates -----


def test_gather_compares_with_last_week(atlas: Session, two_weeks):
    week5, week6 = two_weeks
    data = report.gather(atlas, week6)
    before = {a.epic_key: a.rating for a in runs.assessments(atlas, week5.id)}
    assert data.previous_as_of == week5.as_of
    assert [e.key for e in data.epics if e.done] == ["ATL-4"]
    assert data.overall == "red"
    for epic in data.epics:
        assert epic.previous_rating == before[epic.key]
        assert epic.trend == runs.trend(before[epic.key], epic.rating)
    # Risks resolve to the run's stored evidence, URLs included.
    risk = data.epics[0].key_risks[0]
    assert risk.evidence and all(ev.url and ev.summary for ev in risk.evidence)


def test_a_run_two_days_earlier_is_not_last_week(atlas: Session):
    assess_at(atlas, "2026-02-11")
    data = report.gather(atlas, assess_at(atlas, WEEK6))
    assert data.previous_as_of is None
    assert {e.trend for e in data.epics} == {"new"}


def test_runs_from_another_model_are_not_compared(atlas: Session):
    other = LLM("other-model", dry_run=True)
    assess.run(atlas, "atlas", snapshot.parse_as_of(WEEK5), other)
    atlas.flush()
    atlas.query(Run).filter(Run.model == "dry-run").update({"model": "other-model"})
    data = report.gather(atlas, assess_at(atlas, WEEK6))
    assert data.previous_as_of is None


@pytest.mark.parametrize("variant", report.VARIANTS)
def test_template_reports_fit_their_limits(atlas: Session, two_weeks, variant):
    data = report.gather(atlas, two_weeks[1])
    markdown = report.render_template(data, variant)
    assert 0 < report.word_count(markdown) <= report.WORD_LIMITS[variant]
    # The template must pass the same checks as a model draft.
    system, user = report.render_prompt(data, variant)
    report.validate(markdown, system + user, data, variant)


def test_exec_template_content(atlas: Session, two_weeks):
    markdown = report.render_template(report.gather(atlas, two_weeks[1]), "exec")
    assert "**Overall: RED**" in markdown
    assert "## Top risks" in markdown and "## Decisions needed" in markdown
    assert "ATL-1 Payments API" in markdown


def test_eng_template_covers_every_epic_with_links(atlas: Session, two_weeks):
    markdown = report.render_template(report.gather(atlas, two_weeks[1]), "eng")
    for key in ("ATL-1", "ATL-2", "ATL-3", "ATL-4", "ATL-5"):
        assert key in markdown
    assert "](https://" in markdown
    assert "## Completed" in markdown


def test_word_count_ignores_link_targets():
    assert report.word_count("See [the PR](https://github.example/a/b/pull/1) now.") == 4
    assert report.word_count("# Title\n\n- **bold** item\n---") == 3


# ----- validation -----


@pytest.fixture
def week6_data(atlas: Session):
    data = report.gather(atlas, assess_at(atlas, WEEK6))
    system, user = report.render_prompt(data, "eng")
    return data, system + user


def test_validate_accepts_fenced_markdown(week6_data):
    data, source = week6_data
    text = "```markdown\n# Update\nATL-1 ATL-2 ATL-3 ATL-4 ATL-5 are covered.\n```"
    assert report.validate(text, source, data, "eng") == "# Update\nATL-1 ATL-2 ATL-3 ATL-4 ATL-5 are covered.\n"


@pytest.mark.parametrize("text, problem", [
    ("", "empty"),
    ("word " * 201, "201 words; the limit is 200"),
    ("ATL-1 depends on ATL-999.", "ticket keys that are not in the input: ATL-999"),
    ("See [this](https://evil.example/x).", "URLs that are not in the input: https://evil.example/x"),
    ("Cited evt_deadbeef0000.", "evidence ids that are not in the input: evt_deadbeef0000"),
])
def test_validate_rejects_new_facts_and_long_drafts(week6_data, text, problem):
    data, source = week6_data
    with pytest.raises(report.InvalidReport, match=problem):
        report.validate(text, source, data, "exec")


def test_eng_report_must_cover_every_epic(week6_data):
    data, source = week6_data
    with pytest.raises(report.InvalidReport, match="leaves out epics: ATL-3, ATL-5, ATL-4"):
        report.validate("ATL-1 and ATL-2 are red.", source, data, "eng")


# ----- generation -----


def test_dry_run_returns_the_template(atlas: Session, two_weeks):
    r = report.generate(atlas, two_weeks[1], dry(), "exec")
    assert r.source == "template" and r.error is None and r.attempts == 1
    assert r.markdown == report.render_template(report.gather(atlas, two_weeks[1]), "exec")


def test_invalid_draft_is_retried_with_the_problem(atlas: Session):
    run = assess_at(atlas, WEEK6)
    llm = LLM("test-model", dry_run=True, scripted=["ATL-1 is blocked by ATL-999.", "# Fine\nATL-1 is red."])
    seen: list[list[dict]] = []
    original = llm.complete
    llm.complete = lambda *a, **kw: (seen.append([dict(m) for m in a[2]]), original(*a, **kw))[1]  # type: ignore[method-assign]
    r = report.generate(atlas, run, llm, "exec")
    assert r.markdown == "# Fine\nATL-1 is red.\n" and r.attempts == 2 and r.error is None
    assert "ATL-999" in seen[1][-1]["content"]


def test_second_invalid_draft_falls_back_to_the_template(atlas: Session):
    run = assess_at(atlas, WEEK6)
    llm = LLM("test-model", dry_run=True, scripted=["word " * 300, "word " * 300])
    r = report.generate(atlas, run, llm, "exec")
    assert r.attempts == 2 and "limit is 200" in r.error
    assert r.markdown == report.render_template(report.gather(atlas, run), "exec")


def test_llm_error_falls_back_to_the_template(atlas: Session):
    run = assess_at(atlas, WEEK6)
    r = report.generate(atlas, run, FailingLLM("test-model"), "eng")
    assert r.source == "template" and "overloaded" in r.error


# ----- CLI, end to end (SPEC §15) -----


runner = CliRunner()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PEWA_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_end_to_end_ingest_assess_report(db):
    assert runner.invoke(app, ["ingest", "--fixtures", "atlas"]).exit_code == 0
    assert runner.invoke(app, ["assess", "--as-of", WEEK5, "--dry-run"]).exit_code == 0
    assert runner.invoke(app, ["assess", "--as-of", WEEK6, "--dry-run"]).exit_code == 0

    result = runner.invoke(app, ["report", "--as-of", WEEK6, "--out", "update.md"])
    assert result.exit_code == 0, result.output
    assert "running with --dry-run" in result.output
    markdown = (db / "update.md").read_text()
    assert "# Atlas status, as of Fri 13 Feb 2026" in markdown
    assert "# Atlas engineering update" in markdown
    assert "\n---\n" in markdown

    result = runner.invoke(app, ["report", "--variant", "exec", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "**Overall: RED**" in result.output and "engineering update" not in result.output


def test_report_without_a_run_explains(db):
    assert runner.invoke(app, ["ingest", "--fixtures", "atlas"]).exit_code == 0
    result = runner.invoke(app, ["report", "--as-of", WEEK6, "--dry-run"])
    assert result.exit_code == 1
    assert "pewa assess --as-of 2026-02-13" in result.output
