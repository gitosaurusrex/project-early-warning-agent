"""SPEC §7 / §15: live connectors against mocked HTTP (respx): mapping, pagination, 429s, ingest."""

import json
from datetime import UTC, date, datetime

import httpx
import pytest
import respx
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from pewa import signals, snapshot
from pewa.cli import app
from pewa.connectors.base import ConnectorError
from pewa.connectors.github import GitHubConnector
from pewa.connectors.http import HttpClient
from pewa.connectors.jira import JiraConnector
from pewa.connectors.slack import SlackConnector
from pewa.ingest import derive_outcomes, ingest

JIRA = "https://acme.atlassian.net"
GH = "https://api.github.com"
SLACK = "https://slack.com/api"
SECRET = "s3cret-token-value"


def client(name: str, base: str, sleeps: list[float] | None = None, **kw) -> HttpClient:
    return HttpClient(name, base, sleep=(sleeps.append if sleeps is not None else lambda s: None), **kw)


# ----- shared HTTP client -----


@respx.mock
def test_429_is_retried_after_retry_after():
    route = respx.get(f"{GH}/x").mock(side_effect=[httpx.Response(429, headers={"Retry-After": "2"}),
                                                   httpx.Response(200, json={"ok": 1})])
    sleeps: list[float] = []
    assert client("github", GH, sleeps).get_json("/x") == {"ok": 1}
    assert sleeps == [2.0] and route.call_count == 2


@respx.mock
def test_server_errors_and_network_errors_back_off_exponentially():
    respx.get(f"{GH}/x").mock(side_effect=[httpx.Response(502), httpx.ConnectError("boom"), httpx.Response(200, json=[])])
    sleeps: list[float] = []
    assert client("github", GH, sleeps, backoff=0.5).get_json("/x") == []
    assert sleeps == [0.5, 1.0]


@respx.mock
def test_github_primary_rate_limit_waits_for_reset():
    reset = datetime.now(UTC).timestamp() + 5
    respx.get(f"{GH}/x").mock(side_effect=[
        httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}),
        httpx.Response(200, json={}),
    ])
    sleeps: list[float] = []
    client("github", GH, sleeps).get("/x")
    assert 4 < sleeps[0] <= 7


@respx.mock
def test_rate_limit_reset_too_far_away_fails_clearly():
    respx.get(f"{GH}/x").respond(429, headers={"Retry-After": "3600"})
    with pytest.raises(ConnectorError, match="resets in 3600s"):
        client("github", GH).get("/x")


@respx.mock
def test_retries_run_out():
    respx.get(f"{GH}/x").respond(503)
    with pytest.raises(ConnectorError, match="after 2 retries"):
        client("github", GH, max_retries=2).get("/x")


@respx.mock
def test_auth_errors_name_the_problem_but_never_the_token(caplog):
    respx.get(f"{GH}/x").respond(401)
    c = client("github", GH, headers={"Authorization": f"Bearer {SECRET}"})
    with pytest.raises(ConnectorError, match="access denied") as exc:
        c.get("/x")
    assert SECRET not in str(exc.value) and SECRET not in caplog.text


# ----- Jira -----


def history(when: str, author: str, *items: dict) -> dict:
    return {"id": when, "created": when, "author": {"displayName": author}, "items": list(items)}


def item(field: str, old, new, field_id: str | None = None, raw_from=None, raw_to=None) -> dict:
    return {"field": field, "fieldId": field_id or field, "fromString": old, "toString": new,
            "from": raw_from, "to": raw_to}


EPIC = {
    "key": "ABC-1",
    "fields": {"summary": "Billing v2", "status": {"name": "Done"}, "assignee": {"displayName": "Dana Diaz"},
               "issuetype": {"name": "Epic"}, "parent": None, "duedate": "2026-03-06",
               "created": "2026-01-05T09:00:00.000+0000", "reporter": {"displayName": "Rosa Lee"}},
    "changelog": {"total": 3, "histories": [
        history("2026-01-06T10:00:00.000+0000", "Dana Diaz", item("status", "Backlog", "In Development")),
        history("2026-02-17T15:30:00.000+0000", "Rosa Lee",
                item("duedate", "2026-02-20 00:00:00.0", "2026-03-06 00:00:00.0", raw_from="2026-02-20",
                     raw_to="2026-03-06")),
        history("2026-03-06T16:00:00.000+0000", "Dana Diaz", item("status", "In Development", "Done")),
    ]},
}
STORY = {
    "key": "ABC-2",
    "fields": {"summary": "Invoice export", "status": {"name": "Code Review"}, "assignee": {"displayName": "Sam Ito"},
               "issuetype": {"name": "Story"}, "parent": {"key": "ABC-1"}, "duedate": None,
               "created": "2026-01-06T11:00:00.000+0000", "reporter": {"displayName": "Dana Diaz"},
               "customfield_10016": 5.0},
    # The search only embeds part of a long changelog; the rest comes from the changelog endpoint.
    "changelog": {"total": 4, "histories": []},
}
STORY_CHANGELOG = [
    [history("2026-01-07T09:00:00.000+0000", "Dana Diaz", item("IssueParentAssociation", None, "ABC-1"),
             item("Story point estimate", "3", "5", field_id="customfield_10016")),
     history("2026-01-08T09:00:00.000+0000", "Sam Ito", item("status", "Backlog", "In Development"))],
    [history("2026-01-20T09:00:00.000+0000", "Dana Diaz", item("assignee", "Dana Diaz", "Sam Ito")),
     history("2026-01-21T09:00:00.000+0000", "Sam Ito", item("status", "In Development", "Code Review"),
             item("Sprint", "Sprint 1", "Sprint 2"))],
]
STATUSES = [{"name": n, "statusCategory": {"key": k}} for n, k in
            [("Backlog", "new"), ("In Development", "indeterminate"), ("Code Review", "indeterminate"), ("Done", "done")]]


def mock_jira(router: respx.MockRouter, seen_jql: list[str] | None = None) -> None:
    router.get(f"{JIRA}/rest/api/3/status").respond(json=STATUSES)

    def search(request: httpx.Request) -> httpx.Response:
        if seen_jql is not None:
            seen_jql.append(request.url.params["jql"])
        assert request.url.params["expand"] == "changelog"
        if request.url.params.get("nextPageToken") == "p2":
            return httpx.Response(200, json={"issues": [STORY], "isLast": True})
        return httpx.Response(200, json={"issues": [EPIC], "nextPageToken": "p2", "isLast": False})

    def changelog(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["startAt"])
        page = STORY_CHANGELOG[0] if start == 0 else STORY_CHANGELOG[1]
        return httpx.Response(200, json={"values": page, "startAt": start, "total": 4, "isLast": start > 0})

    router.get(f"{JIRA}/rest/api/3/search/jql").mock(side_effect=search)
    router.get(f"{JIRA}/rest/api/3/issue/ABC-2/changelog").mock(side_effect=changelog)


def jira(**kw) -> JiraConnector:
    return JiraConnector(JIRA, "me@acme.test", SECRET, "ABC", client=client("jira", JIRA), **kw)


@respx.mock
def test_jira_replays_changelog_into_events():
    mock_jira(respx.mock)
    events = jira().fetch(None)
    story = [e for e in events if e.entity_id == "ABC-2"]
    created = story[0]
    assert created.kind == "ticket_created" and created.occurred_at == datetime(2026, 1, 6, 11, tzinfo=UTC)
    # State at creation, rebuilt by walking the changelog backwards from today's fields.
    assert created.payload == {"issue_type": "Story", "title": "Invoice export", "status": "To Do",
                               "assignee": "Dana Diaz", "parent": None, "points": 3.0, "due_date": None}
    assert [(e.kind, e.payload["from"], e.payload["to"]) for e in story[1:]] == [
        ("parent_changed", None, "ABC-1"), ("points_changed", 3.0, 5.0), ("status_changed", "To Do", "In Progress"),
        ("assignee_changed", "Dana Diaz", "Sam Ito"), ("status_changed", "In Progress", "In Review"),
        ("sprint_changed", "Sprint 1", "Sprint 2"),
    ]
    assert story[5].payload["to_name"] == "Code Review" and story[5].actor == "Sam Ito"
    assert created.url == f"{JIRA}/browse/ABC-2"

    epic = [e for e in events if e.entity_id == "ABC-1"]
    assert epic[0].payload["due_date"] == "2026-02-20" and epic[0].payload["issue_type"] == "Epic"
    assert [(e.kind, e.payload["to"]) for e in epic[1:]] == [
        ("status_changed", "In Progress"), ("due_date_changed", "2026-03-06"), ("status_changed", "Done")]


@respx.mock
def test_jira_status_map_overrides_categories():
    mock_jira(respx.mock)
    events = jira(status_map={"In Development": "In Review"}).fetch(None)
    assert ("To Do", "In Review") in [(e.payload["from"], e.payload["to"]) for e in events if e.kind == "status_changed"]
    # In Development -> Code Review now maps In Review -> In Review: no change, no event.
    assert not any(e.payload.get("to_name") == "Code Review" for e in events)


@respx.mock
def test_jira_incremental_sync_looks_back_a_day_then_filters():
    seen: list[str] = []
    mock_jira(respx.mock, seen)
    since = datetime(2026, 2, 17, 15, 30, tzinfo=UTC)
    events = jira().fetch(since)
    assert seen[0] == 'project = "ABC" AND updated >= "2026/02/16 15:30" ORDER BY created ASC'
    assert {(e.entity_id, e.kind) for e in events} == {("ABC-1", "due_date_changed"), ("ABC-1", "status_changed")}
    assert all(e.occurred_at >= since for e in events)


# ----- GitHub -----


def pr(number: int, updated: str, **extra) -> dict:
    return {"number": number, "title": f"ABC-2: change {number}", "html_url": f"https://github.com/acme/api/pull/{number}",
            "user": {"login": "sam"}, "head": {"ref": f"sam/abc-2-{number}"}, "body": "", "created_at": "2026-01-20T10:00:00Z",
            "updated_at": updated, "merged_at": None, **extra}


def mock_github(router: respx.MockRouter) -> None:
    page2 = f"{GH}/repos/acme/api/pulls?page=2"

    def pulls(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "3":  # only reached on a full sync
            return httpx.Response(200, json=[])
        if request.url.params.get("page") == "2":
            # PR 10 was last updated before the sync window, so listing stops there (no page 3).
            return httpx.Response(200, json=[pr(11, "2026-02-01T00:00:00Z"), pr(10, "2026-01-01T00:00:00Z")],
                                  headers={"Link": f'<{GH}/repos/acme/api/pulls?page=3>; rel="next"'})
        assert request.url.params["sort"] == "updated" and request.url.params["state"] == "all"
        return httpx.Response(200, json=[pr(12, "2026-02-10T00:00:00Z", merged_at="2026-02-09T12:00:00Z")],
                              headers={"Link": f'<{page2}>; rel="next", <{page2}>; rel="last"'})

    router.get(f"{GH}/repos/acme/api/pulls").mock(side_effect=pulls)
    router.get(f"{GH}/repos/acme/api/issues/12/timeline").respond(json=[
        {"event": "review_requested", "created_at": "2026-01-20T10:05:00Z", "actor": {"login": "sam"},
         "requested_reviewer": {"login": "kim"}},
        {"event": "reviewed", "submitted_at": "2026-02-03T09:00:00Z", "user": {"login": "kim"}, "state": "CHANGES_REQUESTED"},
        {"event": "committed", "sha": "abc"},
        {"event": "reviewed", "submitted_at": "2026-02-09T11:00:00Z", "user": {"login": "kim"}, "state": "APPROVED"},
        {"event": "merged", "created_at": "2026-02-09T12:00:00Z", "actor": {"login": "sam"}},
        {"event": "closed", "created_at": "2026-02-09T12:00:00Z", "actor": {"login": "sam"}},
    ])
    router.get(f"{GH}/repos/acme/api/issues/11/timeline").respond(json=[
        {"event": "closed", "created_at": "2026-02-01T00:00:00Z", "actor": {"login": "sam"}}])
    router.get(f"{GH}/repos/acme/api/pulls/12/commits").respond(json=[{"sha": "a" * 40}, {"sha": "b" * 40}])
    router.get(f"{GH}/repos/acme/api/pulls/11/commits").respond(json=[])
    router.get(f"{GH}/repos/acme/api/issues/10/timeline").respond(json=[])  # full syncs only
    router.get(f"{GH}/repos/acme/api/pulls/10/commits").respond(json=[])
    router.get(f"{GH}/repos/acme/api/commits/{'a' * 40}/check-runs").respond(json={"check_runs": [
        {"name": "test", "conclusion": "failure", "completed_at": "2026-02-02T08:00:00Z", "app": {"slug": "github-actions"}},
        {"name": "lint", "conclusion": "success", "completed_at": "2026-02-02T08:00:00Z"},
    ]})
    router.get(f"{GH}/repos/acme/api/commits/{'b' * 40}/check-runs").respond(json={"check_runs": [
        {"name": "test", "conclusion": "timed_out", "completed_at": "2026-02-05T08:00:00Z"}]})


@respx.mock
def test_github_pull_requests_become_events():
    mock_github(respx.mock)
    events = GitHubConnector(SECRET, ["acme/api"], client=client("github", GH)).fetch(datetime(2026, 1, 15, tzinfo=UTC))
    by_pr = {k: [e for e in events if e.entity_id == f"acme/api#{k}"] for k in (10, 11, 12)}
    assert by_pr[10] == []  # outside the window: never fetched
    assert [e.kind for e in by_pr[12]] == ["pr_opened", "pr_review_requested", "pr_reviewed", "pr_reviewed",
                                           "pr_merged", "ci_failed", "ci_failed"]
    opened = by_pr[12][0]
    assert opened.payload == {"repo": "acme/api", "number": 12, "title": "ABC-2: change 12", "branch": "sam/abc-2-12",
                              "body": ""}
    assert by_pr[12][1].payload == {"reviewer": "kim"} and by_pr[12][2].payload == {"state": "changes_requested"}
    assert [e.payload["check"] for e in by_pr[12] if e.kind == "ci_failed"] == ["test", "test"]
    assert [e.kind for e in by_pr[11]] == ["pr_opened", "pr_closed"]  # closed without merging


@respx.mock
def test_github_missing_repo_is_a_clear_error():
    respx.get(f"{GH}/repos/acme/nope/pulls").respond(404)
    with pytest.raises(ConnectorError, match="not found"):
        GitHubConnector(SECRET, ["acme/nope"], client=client("github", GH)).fetch(None)


# ----- Slack -----


def msg(ts: str, text: str, user: str = "U1", **extra) -> dict:
    return {"type": "message", "ts": ts, "text": text, "user": user, **extra}


ROOT_TS = "1770200000.000100"  # 2026-02-04


def mock_slack(router: respx.MockRouter, seen: list[dict] | None = None) -> None:
    router.get(f"{SLACK}/auth.test").respond(json={"ok": True, "url": "https://acme.slack.com/"})
    router.get(f"{SLACK}/conversations.info").respond(json={"ok": True, "channel": {"id": "C1", "name": "eng"}})

    def history(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(dict(request.url.params))
        if request.url.params.get("cursor") == "c2":
            return httpx.Response(200, json={"ok": True, "messages": [
                msg("1770000000.000200", "lunch?"), msg("1770000001.000300", "", subtype="channel_join")]})
        return httpx.Response(200, json={"ok": True, "messages": [
            msg(ROOT_TS, "ABC-2 is blocked on review", thread_ts=ROOT_TS, reply_count=2)],
            "response_metadata": {"next_cursor": "c2"}})

    router.get(f"{SLACK}/conversations.history").mock(side_effect=history)
    router.get(f"{SLACK}/conversations.replies").respond(json={"ok": True, "messages": [
        msg(ROOT_TS, "ABC-2 is blocked on review", thread_ts=ROOT_TS, reply_count=2),
        msg("1770200100.000200", "still waiting on legal", user="U2", thread_ts=ROOT_TS),
        msg("1770200200.000300", "deploy bot here", user=None, bot_profile={"name": "deploybot"}, thread_ts=ROOT_TS),
    ]})


@respx.mock
def test_slack_history_and_threads_become_events():
    mock_slack(respx.mock)
    events = SlackConnector(SECRET, ["C1"], client=client("slack", SLACK)).fetch(None)
    assert [e.payload["text"] for e in events] == ["ABC-2 is blocked on review", "still waiting on legal",
                                                   "deploy bot here", "lunch?"]  # channel_join skipped
    root, reply, bot = events[:3]
    assert root.entity_id == f"C1:{ROOT_TS}" and root.payload["channel_name"] == "eng"
    assert root.url == "https://acme.slack.com/archives/C1/p1770200000000100"
    assert reply.url.endswith(f"?thread_ts={ROOT_TS}&cid=C1") and reply.payload["thread_ts"] == ROOT_TS
    assert bot.actor == "deploybot"
    assert root.occurred_at == datetime.fromtimestamp(1770200000.0001, UTC)


@respx.mock
def test_slack_incremental_sync_rereads_recent_threads():
    seen: list[dict] = []
    mock_slack(respx.mock, seen)
    since = datetime(2026, 2, 4, 10, 15, tzinfo=UTC)
    events = SlackConnector(SECRET, ["C1"], client=client("slack", SLACK)).fetch(since)
    assert float(seen[0]["oldest"]) == pytest.approx(since.timestamp() - 14 * 86400)
    assert [e.payload["text"] for e in events] == ["still waiting on legal", "deploy bot here"]


@respx.mock
def test_slack_errors_include_a_hint():
    respx.get(f"{SLACK}/auth.test").respond(json={"ok": True, "url": "https://acme.slack.com/"})
    respx.get(f"{SLACK}/conversations.info").respond(json={"ok": False, "error": "not_in_channel"})
    with pytest.raises(ConnectorError, match="not_in_channel; invite the bot"):
        SlackConnector(SECRET, ["C1"], client=client("slack", SLACK)).fetch(None)


@respx.mock
def test_slack_429_is_retried():
    mock_slack(respx.mock)
    respx.get(f"{SLACK}/auth.test").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(200, json={"ok": True, "url": ""})])
    sleeps: list[float] = []
    events = SlackConnector(SECRET, ["C1"], client=client("slack", SLACK, sleeps)).fetch(None)
    assert sleeps == [1.0] and events and events[0].url is None  # no workspace URL: no links


# ----- end to end: ingest, snapshot, signals -----


@respx.mock
def test_live_sources_ingest_into_linked_snapshots(session: Session):
    mock_jira(respx.mock)
    mock_github(respx.mock)
    mock_slack(respx.mock)
    connectors = [jira(), GitHubConnector(SECRET, ["acme/api"], client=client("github", GH)),
                  SlackConnector(SECRET, ["C1"], client=client("slack", SLACK))]
    result = ingest(session, "acme", connectors)
    assert result.errors == {} and all(result.inserted.values())

    state = snapshot.build(session, "acme", snapshot.parse_as_of("2026-02-13"))
    assert state.epics["ABC-1"].children == ["ABC-2"]
    assert state.tickets["ABC-2"].status == "In Review" and state.tickets["ABC-2"].assignee == "Sam Ito"
    assert state.prs["acme/api#12"].epic_key == "ABC-1"
    linked = {e.payload.get("text"): e.epic_key for e in state.events if e.source == "slack"}
    assert linked["still waiting on legal"] == "ABC-1"  # thread reply inherits the root's link
    assert linked["lunch?"] is None

    found = {s.type for s in signals.detect(state)["ABC-1"]}
    assert {"blocker_language", "ci_flaky"} <= found

    later = snapshot.build(session, "acme", snapshot.parse_as_of("2026-02-20"))
    assert later.epics["ABC-1"].due_date == "2026-03-06" and later.epics["ABC-1"].original_due_date == "2026-02-20"
    assert "due_date_pushed" in {s.type for s in signals.detect(later)["ABC-1"]}

    assert derive_outcomes(session, "acme") == [
        {"epic_key": "ABC-1", "planned_date": date(2026, 2, 20), "actual_date": date(2026, 3, 6), "slipped": True}]


@respx.mock
def test_a_failing_source_does_not_block_the_others(session: Session):
    mock_jira(respx.mock)
    respx.get(f"{SLACK}/auth.test").respond(json={"ok": False, "error": "invalid_auth"})
    result = ingest(session, "acme", [jira(), SlackConnector(SECRET, ["C1"], client=client("slack", SLACK))])
    assert result.inserted["jira"] > 0 and "invalid_auth" in result.errors["slack"]


# ----- CLI -----

runner = CliRunner()
LIVE_ENV = {"JIRA_BASE_URL": JIRA, "JIRA_EMAIL": "me@acme.test", "JIRA_API_TOKEN": SECRET, "JIRA_PROJECT_KEY": "ABC",
            "GITHUB_TOKEN": SECRET, "GITHUB_REPOS": "acme/api", "SLACK_BOT_TOKEN": SECRET, "SLACK_CHANNELS": "C1"}


@pytest.fixture
def live_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PEWA_DB_PATH", str(tmp_path / "live.db"))
    monkeypatch.chdir(tmp_path)
    for key, value in LIVE_ENV.items():
        monkeypatch.setenv(key, value)
    return tmp_path


@respx.mock
def test_cli_ingests_live_sources(live_env, monkeypatch):
    mock_jira(respx.mock)
    mock_github(respx.mock)
    mock_slack(respx.mock)
    monkeypatch.setenv("PEWA_PROJECT", "acme")
    result = runner.invoke(app, ["ingest"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "skipping" not in result.output and SECRET not in result.output

    result = runner.invoke(app, ["outcomes", "--derive"])
    assert result.exit_code == 0, result.output
    assert "Saved 1 outcome(s) for 'acme'" in result.output and "ABC-1" in result.output


def test_cli_refuses_to_mix_live_data_into_a_fixture_project(live_env):
    result = runner.invoke(app, ["ingest"])
    assert result.exit_code == 1 and "bundled fixture dataset" in result.output


@respx.mock
def test_cli_reports_a_failing_source(live_env, monkeypatch):
    mock_jira(respx.mock)
    respx.get(f"{SLACK}/auth.test").respond(json={"ok": False, "error": "invalid_auth"})
    monkeypatch.delenv("GITHUB_TOKEN")
    result = runner.invoke(app, ["ingest", "--project", "acme"], env={"COLUMNS": "200"})
    assert result.exit_code == 1
    assert "skipping github: credentials not configured" in result.output
    assert "slack was skipped: slack: auth.test failed: invalid_auth" in result.output
    assert runner.invoke(app, ["snapshot", "--as-of", "2026-02-13", "--project", "acme"]).exit_code == 0  # Jira kept


def test_cli_rejects_unknown_status_mapping(live_env, monkeypatch):
    monkeypatch.setenv("JIRA_STATUS_MAP", "Code Review=Reviewing")
    result = runner.invoke(app, ["ingest", "--source", "jira", "--project", "acme"])
    assert result.exit_code == 1 and "unknown statuses ['Reviewing']" in result.output


def test_cli_outcomes_from_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PEWA_DB_PATH", str(tmp_path / "o.db"))
    path = tmp_path / "outcomes.json"
    path.write_text(json.dumps([{"epic_key": "X-1", "planned_date": "2026-02-20", "actual_date": None, "slipped": True}]))
    assert runner.invoke(app, ["outcomes", "--project", "p"]).exit_code == 1
    result = runner.invoke(app, ["outcomes", "--file", str(path), "--project", "p"])
    assert result.exit_code == 0, result.output
    assert "X-1" in result.output and "yes" in result.output
    path.write_text("{not json")
    assert runner.invoke(app, ["outcomes", "--file", str(path), "--project", "p"]).exit_code == 1
