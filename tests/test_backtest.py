"""SPEC §12 / Phase 4: replay, metrics, reports, comparison, and the CLI and dashboard views."""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from pewa import backtest, runs
from pewa.cli import app
from pewa.connectors.fixtures import load_outcomes
from pewa.ingest import save_outcomes
from pewa.llm import LLM
from pewa.models import Assessment, Backtest, BacktestStep, Feedback, Outcome, Run
from pewa.web.app import create_app

ATLAS_FROM, ATLAS_TO = date(2026, 1, 5), date(2026, 3, 13)


# ----- step parsing -----


@pytest.mark.parametrize("value, days", [("7d", 7), ("1w", 7), (" 2W ", 14), ("3d", 3)])
def test_parse_step(value, days):
    assert backtest.parse_step(value) == days


@pytest.mark.parametrize("value", ["7", "0d", "week", "-1d"])
def test_parse_step_rejects(value):
    with pytest.raises(ValueError):
        backtest.parse_step(value)


def test_step_dates_stop_at_or_before_the_end():
    assert backtest.step_dates(ATLAS_FROM, ATLAS_TO, 7)[-1] == date(2026, 3, 9)
    assert len(backtest.step_dates(ATLAS_FROM, ATLAS_TO, 7)) == 10
    assert backtest.step_dates(ATLAS_FROM, ATLAS_FROM, 7) == [ATLAS_FROM]
    with pytest.raises(ValueError, match="before"):
        backtest.step_dates(ATLAS_TO, ATLAS_FROM, 7)


# ----- Phase 4 acceptance: the Atlas backtest -----


@pytest.fixture
def atlas_backtest(atlas: Session):
    save_outcomes(atlas, "atlas", load_outcomes("atlas"))
    seen: list[date] = []
    bt = backtest.run(atlas, "atlas", ATLAS_FROM, ATLAS_TO, 7, LLM("test", dry_run=True, session=atlas), label="baseline",
                      on_step=lambda day, result: seen.append(day))
    return bt, backtest.evaluate(atlas, bt), seen


def test_atlas_backtest_metrics(atlas: Session, atlas_backtest):
    bt, ev, seen = atlas_backtest
    assert seen == ev.steps and len(ev.steps) == 10
    by_key = {e.key: e for e in ev.epics}
    # Epic A (due Fri 20 Feb) is flagged on Mon 9 Feb, 11 days early; B (due 6 Mar) on 9 Feb too.
    assert by_key["ATL-1"].first_flag == date(2026, 2, 9) and by_key["ATL-1"].lead_time_days == 11
    assert by_key["ATL-2"].lead_time_days == 25
    assert [by_key[k].verdict for k in ("ATL-3", "ATL-4", "ATL-5")] == ["correctly quiet"] * 3
    assert (ev.tp, ev.fp, ev.fn, ev.tn) == (2, 0, 0, 3)
    assert ev.precision == ev.recall == 1.0 and ev.false_alarm_rate == 0.0
    assert ev.lead_time_mean == 18
    assert ev.sustained_lead_times == [11, 25]  # the dry-run responder never withdraws a flag
    assert ev.citations_total > 0 and ev.citation_validity == 1.0
    assert ev.assessments == 50 and ev.unknown_assessments == 0
    assert bt.model == "dry-run" and bt.llm_calls > 0 and bt.cost_usd == 0


def test_backtest_runs_are_kept_out_of_live_views(atlas: Session, atlas_backtest):
    bt, _, _ = atlas_backtest
    stored = atlas.scalars(select(Run).join(BacktestStep, BacktestStep.run_id == Run.id)
                           .where(BacktestStep.backtest_id == bt.id)).all()
    assert len(stored) == 10 and {r.mode for r in stored} == {"backtest"} and {r.label for r in stored} == {"baseline"}
    assert runs.latest_run(atlas, "atlas") is None  # live overview and reports ignore backtest runs


def test_done_epics_show_as_completed(atlas_backtest):
    _, ev, _ = atlas_backtest
    atl4 = next(e for e in ev.epics if e.key == "ATL-4")  # shipped Thu 12 Feb
    assert [c.done for c in atl4.cells] == [False] * 6 + [True] * 4


# ----- scoring rules on hand-built runs -----


def add_step(session: Session, bt: Backtest, day: date, ratings: dict[str, str], **detail) -> None:
    as_of = datetime(day.year, day.month, day.day, 23, 59, tzinfo=UTC)
    run = Run(project="p", as_of=as_of, mode="backtest", model="m")
    session.add(run)
    session.flush()
    for key, rating in ratings.items():
        session.add(Assessment(run_id=run.id, epic_key=key, rating=rating, rationale="r",
                               detail={"title": key, "key_risks": [], **detail}))
    session.add(BacktestStep(backtest_id=bt.id, run_id=run.id, as_of=as_of))
    session.flush()


@pytest.fixture
def hand_built(session: Session) -> Backtest:
    session.add_all([
        Outcome(project="p", epic_key="LATE", planned_date=date(2026, 2, 20), actual_date=date(2026, 3, 6), slipped=True),
        Outcome(project="p", epic_key="FINE", planned_date=date(2026, 2, 20), actual_date=date(2026, 2, 19), slipped=False),
    ])
    bt = Backtest(project="p", start=date(2026, 2, 9), end=date(2026, 2, 23), step_days=7, model="m")
    session.add(bt)
    session.flush()
    add_step(session, bt, date(2026, 2, 9), {"LATE": "green", "FINE": "amber", "NEW": "red"})
    add_step(session, bt, date(2026, 2, 16), {"LATE": "green", "FINE": "green", "NEW": "red"})
    # On the day after LATE's planned date it turns red: too late to count as a warning.
    add_step(session, bt, date(2026, 2, 23), {"LATE": "red", "FINE": "unknown", "NEW": "red"},
             key_risks=[{"summary": "s", "evidence_ids": ["evt_missing"]}], dropped_evidence_ids=["evt_made_up"])
    return bt


def test_flags_after_the_planned_date_are_not_early_warnings(session: Session, hand_built):
    ev = backtest.evaluate(session, hand_built)
    by_key = {e.key: e for e in ev.epics}
    assert by_key["LATE"].first_flag is None and by_key["LATE"].first_red == date(2026, 2, 23)
    assert by_key["LATE"].verdict == "missed"
    assert by_key["FINE"].verdict == "false alarm"
    assert by_key["NEW"].slipped is None and by_key["NEW"].verdict == ""  # no ground truth: not scored
    assert (ev.tp, ev.fp, ev.fn, ev.tn) == (0, 1, 1, 0)
    assert ev.recall == 0.0 and ev.precision == 0.0 and ev.false_alarm_rate == 1.0
    assert ev.lead_time_mean is None
    assert ev.unknown_assessments == 1


def test_sustained_lead_time_ignores_withdrawn_warnings(session: Session):
    session.add(Outcome(project="p", epic_key="E", planned_date=date(2026, 3, 6), actual_date=date(2026, 3, 13),
                        slipped=True))
    bt = Backtest(project="p", start=date(2026, 1, 19), end=date(2026, 2, 23), step_days=7, model="m")
    session.add(bt)
    session.flush()
    for day, rating in [(19, "amber"), (26, "amber"), (33, "green"), (40, "red"), (47, "red"), (54, "amber")]:
        add_step(session, bt, date(2026, 1, 1) + timedelta(days=day - 1), {"E": rating})
    epic = backtest.evaluate(session, bt).epics[0]
    assert epic.first_flag == date(2026, 1, 19) and epic.lead_time_days == 46
    assert epic.sustained_flag == date(2026, 2, 9) and epic.sustained_lead_time_days == 25


def test_citation_validity_counts_missing_and_dropped_ids(session: Session, hand_built):
    ev = backtest.evaluate(session, hand_built)
    # Three assessments each cite one id that doesn't exist and had one made-up id dropped.
    assert (ev.citations_valid, ev.citations_total, ev.citations_dropped) == (0, 6, 3)
    assert ev.citation_validity == 0.0


def test_feedback_on_backtest_assessments_is_counted(session: Session, hand_built):
    ids = session.scalars(select(Assessment.id).order_by(Assessment.id)).all()
    session.add_all([Feedback(assessment_id=ids[0], verdict="useful"), Feedback(assessment_id=ids[1], verdict="noise"),
                     Feedback(assessment_id=ids[1], verdict="noise")])
    ev = backtest.evaluate(session, hand_built)
    assert (ev.feedback_useful, ev.feedback_noise) == (1, 2)


def test_empty_metrics_are_reported_as_na(session: Session):
    bt = Backtest(project="p", start=date(2026, 2, 9), end=date(2026, 2, 9), step_days=7, model="m")
    session.add(bt)
    session.flush()
    ev = backtest.evaluate(session, bt)
    assert {value for _, value, _ in ev.metric_rows()[:6]} == {"n/a"}
    assert "No steps" in backtest.render(ev, "html")


# ----- chart and reports -----


def test_chart_places_cells_and_date_markers(atlas_backtest):
    _, ev, _ = atlas_backtest
    g = backtest.chart(ev, link=True)
    assert [r["epic"].key for r in g["rows"]] == ["ATL-1", "ATL-2", "ATL-3", "ATL-4", "ATL-5"]
    atl1 = g["rows"][0]
    assert len(atl1["cells"]) == 10 and atl1["cells"][0]["href"].startswith("/assessments/")
    assert [c["text"] for c in atl1["cells"]][4:6] == ["G", "R"]
    # Fri 20 Feb sits 4/7 of the way from the Mon 16 Feb cell to the Mon 23 Feb cell.
    centre_16 = g["ticks"][6]["x"]
    assert atl1["planned_x"] == pytest.approx(centre_16 + 4 / 7 * backtest.PITCH)
    assert backtest.chart(ev)["rows"][0]["cells"][0]["href"] is None


def test_reports_render(atlas: Session, atlas_backtest):
    _, ev, _ = atlas_backtest
    markdown = backtest.render(ev, "md")
    assert "| Recall | 100% (2/2) |" in markdown and "| Lead time | 18.0 days (min 11, max 25) |" in markdown
    assert "| ATL-1 | 🟢G |" in markdown and "flagged 11 days early" in markdown
    html = backtest.render(ev, "html")
    assert html.startswith("<!doctype html>") and "<svg class=\"bt-chart\"" in html
    assert "prefers-color-scheme: dark" in html and "Table view" in html


def test_html_report_escapes_rationales(session: Session, hand_built):
    session.execute(Assessment.__table__.update().values(rationale="<script>alert(1)</script>"))
    html = backtest.render(backtest.evaluate(session, hand_built), "html")
    assert "<script>alert" not in html and "&lt;script&gt;" in html


def test_compare_reports(atlas: Session, atlas_backtest):
    _, a, _ = atlas_backtest
    bt2 = backtest.run(atlas, "atlas", date(2026, 1, 9), date(2026, 3, 13), 14, LLM("t", dry_run=True))
    b = backtest.evaluate(atlas, bt2)
    markdown = backtest.render_compare(a, b, "md")
    assert f"| Recall | 100% (2/2) | {b.metric_rows()[2][1]} |" in markdown
    assert "different step dates" in markdown
    html = backtest.render_compare(a, b, "html")
    assert html.count('<svg class="bt-chart"') == 2


# ----- CLI -----

runner = CliRunner()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PEWA_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_cli_backtest_list_rescore_and_compare(db):
    assert runner.invoke(app, ["ingest", "--fixtures", "atlas"]).exit_code == 0
    result = runner.invoke(app, ["backtest", "--from", "2026-01-05", "--to", "2026-03-13", "--step", "7d", "--dry-run",
                                 "--label", "baseline"])
    assert result.exit_code == 0, result.output
    assert "Mon 09 Feb: ATL-1 red" in result.output and "100% (2/2)" in result.output
    assert (db / "backtest_reports" / "backtest-1.md").exists() and (db / "backtest_reports" / "backtest-1.html").exists()

    # Without dates it spans the project's events.
    result = runner.invoke(app, ["backtest", "--step", "2w", "--out", "elsewhere"])
    assert result.exit_code == 0, result.output
    assert "running with --dry-run" in result.output and (db / "elsewhere" / "backtest-2.html").exists()

    result = runner.invoke(app, ["backtests"], env={"COLUMNS": "200"})
    assert result.exit_code == 0 and "baseline" in result.output

    result = runner.invoke(app, ["backtests", "1"], env={"COLUMNS": "200"})
    assert result.exit_code == 0 and "flagged 11 days early" in result.output

    result = runner.invoke(app, ["compare", "1", "2"])
    assert result.exit_code == 0, result.output
    assert (db / "backtest_reports" / "compare-1-2.html").exists()

    assert runner.invoke(app, ["compare", "1", "9"]).exit_code == 1
    assert runner.invoke(app, ["backtests", "9"]).exit_code == 1


def test_cli_backtest_errors(db):
    result = runner.invoke(app, ["backtest", "--dry-run"])
    assert result.exit_code == 1 and "pewa ingest" in result.output
    assert runner.invoke(app, ["ingest", "--fixtures", "atlas"]).exit_code == 0
    assert runner.invoke(app, ["backtest", "--step", "weekly", "--dry-run"]).exit_code == 2
    assert runner.invoke(app, ["backtest", "--from", "2026-03-01", "--to", "2026-02-01", "--dry-run"]).exit_code == 2
    assert runner.invoke(app, ["backtest", "--from", "March", "--dry-run"]).exit_code == 2
    result = runner.invoke(app, ["backtests"])
    assert result.exit_code == 1 and "No backtests" in result.output


# ----- dashboard -----


def test_dashboard_backtest_pages(tmp_path):
    from pewa.connectors.fixtures import FixtureConnector
    from pewa.db import make_engine, session_scope
    from pewa.ingest import ingest
    from pewa.schemas import SOURCES

    engine = make_engine(tmp_path / "web.db")
    client = TestClient(create_app(engine, project="atlas", dry_run=True))
    assert "No backtests yet" in client.get("/backtests").text

    with session_scope(engine) as s:
        ingest(s, "atlas", [FixtureConnector("atlas", src) for src in SOURCES])
        save_outcomes(s, "atlas", load_outcomes("atlas"))
        for step in (7, 14):
            backtest.run(s, "atlas", ATLAS_FROM, ATLAS_TO, step, LLM("t", dry_run=True))

    listing = client.get("/backtests").text
    assert "Backtest 1" in listing and "Backtest 2" in listing and "Compare two backtests" in listing

    page = client.get("/backtests/1").text
    assert '<svg class="bt-chart"' in page and "Lead time" in page and "flagged 11 days early" in page
    cell = page.split('<a href="/assessments/')[1].split('"')[0]
    detail = client.get(f"/assessments/{cell}").text
    assert "← Backtest 1" in detail and detail.count('<a href="/assessments/') == 10  # history: this backtest only

    compare = client.get("/backtests/compare?a=1&b=2").text
    assert compare.count('<svg class="bt-chart"') == 2 and "different step dates" in compare
    assert client.get("/backtests/9").status_code == 404
    assert client.get("/backtests/compare?a=1&b=9").status_code == 404
