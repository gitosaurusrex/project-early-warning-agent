from conftest import ev, utc
from pewa.entities import build_versions
from pewa.linking import LinkResolver, pr_keys, ticket_keys
from pewa.schemas import make_event_id

TICKETS = {
    "ATL-1": {"issue_type": "Epic"},
    "ATL-2": {"issue_type": "Story", "parent": "ATL-1"},
    "ATL-3": {"issue_type": "Sub-task", "parent": "ATL-2"},
    "ATL-5": {"issue_type": "Story", "parent": None},
}
PRS = {
    "acme/api#7": {"refs": ["ATL-2"]},
    "acme/api#8": {"refs": ["UTF-8"]},
}


def with_id(event):
    return event.model_copy(update={"id": make_event_id("p", event.source, event.kind, event.entity_id, event.occurred_at)})


def test_ticket_keys_from_title_branch_and_body():
    assert ticket_keys("ATL-12: fix", "diego/atl-13-ledger", "Closes ATL-12 and ATL-14.") == ["ATL-12", "ATL-13", "ATL-14"]
    assert ticket_keys(None, "") == []


def test_pr_keys_from_urls():
    text = "see https://github.example/acme/api/pull/7 and <https://github.com/acme/web/pull/12|this>"
    assert pr_keys(text) == ["acme/api#7", "acme/web#12"]


def test_epic_resolution():
    r = LinkResolver(TICKETS, PRS)
    assert r.epic_for_ticket("ATL-1") == "ATL-1"
    assert r.epic_for_ticket("ATL-2") == "ATL-1"
    assert r.epic_for_ticket("ATL-3") == "ATL-1"  # sub-task -> story -> epic
    assert r.epic_for_ticket("ATL-5") is None
    assert r.epic_for_ticket("NOPE-1") is None
    assert r.epic_for_pr("acme/api#7") == "ATL-1"
    assert r.epic_for_pr("acme/api#8") is None  # ticket-shaped noise is ignored


def test_slack_links_and_thread_inheritance():
    r = LinkResolver(TICKETS, PRS)
    root = with_id(ev("slack", "message", "C1:1.0", utc(2026, 1, 1), channel="C1", ts="1.0", text="PR https://github.example/acme/api/pull/7"))
    reply = with_id(ev("slack", "message", "C1:2.0", utc(2026, 1, 1, 1), channel="C1", ts="2.0", thread_ts="1.0", text="still waiting"))
    other = with_id(ev("slack", "message", "C1:3.0", utc(2026, 1, 1, 2), channel="C1", ts="3.0", text="lunch?"))
    direct = with_id(ev("slack", "message", "C1:4.0", utc(2026, 1, 1, 3), channel="C1", ts="4.0", text="ATL-3 is done"))
    links = r.resolve([root, reply, other, direct])
    assert links == {root.id: "ATL-1", reply.id: "ATL-1", other.id: None, direct.id: "ATL-1"}


def test_versions_collapse_same_instant_and_track_original_due_date():
    t0 = utc(2026, 1, 1, 9)
    events = [
        ev("jira", "ticket_created", "ATL-1", t0, issue_type="Epic", title="E", status="To Do", due_date="2026-02-01"),
        ev("jira", "status_changed", "ATL-1", t0, **{"from": "To Do", "to": "In Progress"}),
        ev("jira", "due_date_changed", "ATL-1", utc(2026, 1, 5), **{"from": "2026-02-01", "to": "2026-02-10"}),
        ev("jira", "comment_added", "ATL-1", utc(2026, 1, 6), body="no state change"),
        ev("jira", "status_changed", "ATL-1", utc(2026, 1, 7), **{"from": "In Progress", "to": "Done"}),
        ev("jira", "status_changed", "ATL-1", utc(2026, 1, 8), **{"from": "Done", "to": "In Progress"}),
    ]
    versions = build_versions(events)
    assert [v[0] for v in versions] == [t0, utc(2026, 1, 5), utc(2026, 1, 7), utc(2026, 1, 8)]
    assert versions[0][1]["status"] == "In Progress"
    assert versions[1][1]["due_date"] == "2026-02-10" and versions[1][1]["original_due_date"] == "2026-02-01"
    assert versions[2][1]["resolved_at"] is not None
    assert versions[3][1]["resolved_at"] is None  # reopened
