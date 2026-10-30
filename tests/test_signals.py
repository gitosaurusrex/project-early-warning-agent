"""SPEC §9 / §15: every detector, on small hand-built event lists."""

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from conftest import ev, ingest_events, utc
from pewa import signals, snapshot
from pewa.config import Settings

MON = utc(2026, 3, 2, 9)  # a Monday
PR = "acme/api#7"


def at(days: int, hour: int = 10) -> datetime:
    return MON.replace(hour=hour) + timedelta(days=days)


def epic(due: str = "2026-04-03", started: int = 0) -> list:
    return [
        ev("jira", "ticket_created", "ATL-1", MON - timedelta(days=7), "rosa", issue_type="Epic", title="Payments",
           status="To Do", assignee="dee", due_date=due),
        ev("jira", "status_changed", "ATL-1", at(started, 9), "dee", **{"from": "To Do", "to": "In Progress"}),
    ]


def ticket(key: str, created: datetime | None = None, points: int = 3, assignee: str = "dee", parent: str = "ATL-1"):
    return ev("jira", "ticket_created", key, created or MON - timedelta(days=6), "dee", issue_type="Story",
              title=f"Story {key}", status="To Do", assignee=assignee, parent=parent, points=points)


def move(key: str, when: datetime, frm: str, to: str, actor: str = "dee"):
    return ev("jira", "status_changed", key, when, actor, **{"from": frm, "to": to})


def pr_opened(when: datetime, key: str = "ATL-2"):
    return ev("github", "pr_opened", PR, when, "dee", repo="acme/api", number=7, title=f"{key}: work", branch="b", body="")


def slack(ts: str, when: datetime, text: str, thread_ts: str | None = None):
    return ev("slack", "message", f"C1:{ts}", when, "dee", channel="C1", ts=ts, thread_ts=thread_ts, text=text)


def detect(session: Session, events: list, as_of: datetime, **overrides) -> list:
    ingest_events(session, "t", events)
    state = snapshot.build(session, "t", as_of)
    return signals.detect(state, Settings(_env_file=None, **overrides)).get("ATL-1", [])


def of_type(found: list, kind: str) -> list:
    return [s for s in found if s.type == kind]


def test_business_days():
    assert signals.business_days(date(2026, 3, 2), date(2026, 3, 6)) == 4  # Mon -> Fri
    assert signals.business_days(date(2026, 3, 6), date(2026, 3, 9)) == 1  # Fri -> Mon
    assert signals.business_days(date(2026, 3, 2), date(2026, 3, 16)) == 10
    assert signals.business_days(date(2026, 3, 6), date(2026, 3, 2)) == 0


# ----- stale_review -----


def review_events():
    return [*epic(), ticket("ATL-2"), pr_opened(at(0)),
            ev("github", "pr_review_requested", PR, at(0, 11), "dee", reviewer="ken")]


def test_stale_review_fires_after_threshold(session):
    found = of_type(detect(session, review_events(), at(4, 17)), "stale_review")  # Fri: 4 business days
    assert len(found) == 1 and found[0].severity == 1
    assert "ken" in found[0].detail
    requested = next(e for e in snapshot.build(session, "t", at(4, 17)).events if e.kind == "pr_review_requested")
    assert requested.id in found[0].evidence


def test_stale_review_quiet_within_threshold(session):
    assert of_type(detect(session, review_events(), at(3, 17)), "stale_review") == []


def test_stale_review_cleared_by_review(session):
    events = [*review_events(), ev("github", "pr_reviewed", PR, at(2), "ken", state="changes_requested")]
    assert of_type(detect(session, events, at(9)), "stale_review") == []


def test_stale_review_escalates(session):
    found = of_type(detect(session, review_events(), at(14)), "stale_review")  # 10 business days
    assert found[0].severity == 3


# ----- stale_ticket -----


def test_stale_ticket_fires_and_cites_last_activity(session):
    events = [*epic(), ticket("ATL-2"), move("ATL-2", at(0), "To Do", "In Progress")]
    found = of_type(detect(session, events, at(8)), "stale_ticket")  # next Tue: 6 business days
    assert len(found) == 1 and "ATL-2" in found[0].detail
    started = next(e for e in snapshot.build(session, "t", at(8)).events if e.kind == "status_changed" and e.entity_id == "ATL-2")
    assert found[0].evidence == [started.id]


def test_stale_ticket_reset_by_slack_mention(session):
    events = [*epic(), ticket("ATL-2"), move("ATL-2", at(0), "To Do", "In Progress"), slack("1.0", at(4), "ATL-2 going fine")]
    assert of_type(detect(session, events, at(8)), "stale_ticket") == []


def test_stale_ticket_ignores_todo_and_done(session):
    events = [*epic(), ticket("ATL-2"), ticket("ATL-3"), move("ATL-3", at(0), "To Do", "Done")]
    assert of_type(detect(session, events, at(20)), "stale_ticket") == []


# ----- reopened -----


def test_reopened_from_done(session):
    events = [*epic(), ticket("ATL-2"), move("ATL-2", at(1), "To Do", "Done"), move("ATL-2", at(3), "Done", "In Progress")]
    found = of_type(detect(session, events, at(4)), "reopened")
    assert len(found) == 1 and found[0].severity == 2 and len(found[0].evidence) == 1


def test_backwards_move_within_workflow(session):
    events = [*epic(), ticket("ATL-2"), move("ATL-2", at(1), "To Do", "In Review"), move("ATL-2", at(2), "In Review", "In Progress")]
    assert of_type(detect(session, events, at(4)), "reopened")[0].severity == 1


def test_reopened_outside_lookback_is_ignored(session):
    events = [*epic(), ticket("ATL-2"), move("ATL-2", at(1), "To Do", "Done"), move("ATL-2", at(2), "Done", "In Progress")]
    assert of_type(detect(session, events, at(30)), "reopened") == []


# ----- due_date_pushed -----


def test_due_date_pushed(session):
    events = [*epic(due="2026-03-20"),
              ev("jira", "due_date_changed", "ATL-1", at(3), "rosa", **{"from": "2026-03-20", "to": "2026-04-03"})]
    found = of_type(detect(session, events, at(4)), "due_date_pushed")
    assert found[0].severity == 3 and "14 days" in found[0].detail


def test_due_date_pulled_in_is_not_a_push(session):
    events = [*epic(due="2026-03-20"),
              ev("jira", "due_date_changed", "ATL-1", at(3), "rosa", **{"from": "2026-03-20", "to": "2026-03-18"})]
    assert of_type(detect(session, events, at(4)), "due_date_pushed") == []


# ----- scope_growth -----


def scope(added: int) -> list:
    before = [ticket(f"ATL-{i}") for i in range(2, 7)]  # 5 tickets before the epic started
    after = [ticket(f"ATL-{i}", created=at(5 + i)) for i in range(7, 7 + added)]
    return [*epic(), *before, *after]


def test_scope_growth_fires_above_threshold(session):
    found = of_type(detect(session, scope(2), at(20)), "scope_growth")  # +40%
    assert found[0].severity == 2 and "5 -> 7" in found[0].detail and len(found[0].evidence) == 2


def test_scope_growth_at_threshold_is_quiet(session):
    assert of_type(detect(session, scope(1), at(20)), "scope_growth") == []  # +20%, not more


def test_scope_growth_counts_reparented_tickets(session):
    events = [*scope(1), ticket("ATL-20", parent="ATL-9"),
              ev("jira", "parent_changed", "ATL-20", at(12), "rosa", **{"from": "ATL-9", "to": "ATL-1"})]
    assert of_type(detect(session, events, at(20)), "scope_growth")[0].severity == 2


# ----- ci_flaky -----


def test_ci_flaky(session):
    events = [*epic(), ticket("ATL-2"), pr_opened(at(0)),
              ev("github", "ci_failed", PR, at(0, 11), "ci", check="test"),
              ev("github", "ci_failed", PR, at(0, 12), "ci", check="test")]
    found = of_type(detect(session, events, at(1)), "ci_flaky")
    assert found[0].severity == 1 and len(found[0].evidence) == 2


def test_single_ci_failure_is_not_flaky(session):
    events = [*epic(), ticket("ATL-2"), pr_opened(at(0)), ev("github", "ci_failed", PR, at(0, 11), "ci", check="test")]
    assert of_type(detect(session, events, at(1)), "ci_flaky") == []


# ----- burn_gap -----


def burn(done_points: int, open_points: int, due: str) -> list:
    events = [*epic(due=due), ticket("ATL-2", points=done_points), ticket("ATL-3", points=open_points),
              move("ATL-2", at(1), "To Do", "In Progress"), move("ATL-2", at(9), "In Progress", "Done")]
    return events


def test_burn_gap_when_pace_cannot_reach_due_date(session):
    found = of_type(detect(session, burn(3, 20, due="2026-03-20"), at(11)), "burn_gap")  # 1.5 pts/wk, 20 open, 1 wk
    assert found[0].severity == 3 and "20 points open" in found[0].detail


def test_burn_gap_quiet_when_on_pace(session):
    assert of_type(detect(session, burn(10, 5, due="2026-03-27"), at(11)), "burn_gap") == []


def test_burn_gap_past_due(session):
    found = of_type(detect(session, burn(3, 2, due="2026-03-10"), at(11)), "burn_gap")
    assert found[0].severity == 3 and "Past due" in found[0].detail


def test_burn_gap_needs_a_week_of_history(session):
    assert of_type(detect(session, burn(3, 20, due="2026-03-04"), at(2)), "burn_gap") == []


# ----- owner_overload -----


def overload(n: int) -> list:
    events = [*epic()]
    for i in range(n):
        key = f"ATL-{i + 2}"
        events += [ticket(key), move(key, at(0, 10 + i), "To Do", "In Progress"), slack(f"{i}.0", at(1, 10 + i), f"{key} ok")]
    return events


def test_owner_overload(session):
    found = of_type(detect(session, overload(4), at(1, 18)), "owner_overload")
    assert found[0].severity == 1 and "4 tickets" in found[0].detail and len(found[0].evidence) == 4


def test_owner_at_limit_is_fine(session):
    assert of_type(detect(session, overload(3), at(1, 18)), "owner_overload") == []


def test_owner_overload_threshold_is_configurable(session):
    assert of_type(detect(session, overload(3), at(1, 18), pewa_owner_overload_n=2), "owner_overload")


# ----- blocker_language -----


def test_blocker_language_in_linked_thread(session):
    events = [*epic(), ticket("ATL-2"), slack("1.0", at(1), "Status of ATL-2?"),
              slack("2.0", at(1, 11), "We're blocked on the infra team", thread_ts="1.0")]
    found = of_type(detect(session, events, at(2)), "blocker_language")
    assert found[0].severity == 1 and "infra team" in found[0].detail


def test_blocker_language_ignores_unlinked_and_neutral(session):
    events = [*epic(), ticket("ATL-2"), slack("1.0", at(1), "ATL-2 is going well"),
              slack("2.0", at(1, 11), "blocked on lunch decisions, as usual")]
    assert of_type(detect(session, events, at(2)), "blocker_language") == []


def test_blocker_language_severity_scales(session):
    events = [*epic(), ticket("ATL-2")] + [slack(f"{i}.0", at(i), f"Still waiting on review for ATL-2 ({i})") for i in range(4)]
    assert of_type(detect(session, events, at(5)), "blocker_language")[0].severity == 3


# ----- general -----


def test_done_epics_get_no_signals(session):
    events = [*epic(due="2026-03-04"), ticket("ATL-2", points=20), move("ATL-1", at(1), "In Progress", "Done")]
    ingest_events(session, "t", events)
    assert "ATL-1" not in signals.detect(snapshot.build(session, "t", at(20)), Settings(_env_file=None))


@pytest.mark.parametrize("as_of", ["2026-01-23", "2026-02-13", "2026-03-06"])
def test_atlas_signal_evidence_is_linked_to_the_epic(atlas, as_of):
    state = snapshot.build(atlas, "atlas", snapshot.parse_as_of(as_of))
    linked = {e.id: e.epic_key for e in state.events}
    for epic_key, found in signals.detect(state).items():
        for signal in found:
            assert signal.evidence, signal
            assert all(linked.get(i) == epic_key for i in signal.evidence), signal
