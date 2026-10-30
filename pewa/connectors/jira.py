"""Jira Cloud connector (SPEC §7): issues and their changelogs, replayed as events.

The search API only returns an issue's *current* fields. To recover history, each issue's
changelog is walked backwards from the current values to rebuild its state at creation
(`ticket_created`), and every change becomes its own event (`status_changed`,
`due_date_changed`, ...), the same shapes the Atlas fixtures use.

Statuses are mapped onto pewa's workflow (To Do / In Progress / In Review / Done): an explicit
JIRA_STATUS_MAP entry wins, then names already in the workflow, then the status category Jira
reports (new / indeterminate / done), with "review" in an in-progress name meaning In Review.
The raw names are kept in the payload.
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from pewa.connectors.http import HttpClient
from pewa.schemas import WORKFLOW, Event, to_utc

log = logging.getLogger(__name__)

PAGE_SIZE = 100
KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
PARENT_FIELDS = {"parent", "issueparentassociation", "epic link"}
POINTS_NAMES = {"story points", "story point estimate"}


def _time(value: str) -> datetime:
    return to_utc(datetime.fromisoformat(value))


def _points(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _key(value: str | None) -> str | None:
    return value if value and KEY_RE.match(value) else None


class JiraConnector:
    source = "jira"

    def __init__(
        self,
        base_url: str,
        email: str,
        token: str,
        project_key: str,
        *,
        points_field: str = "customfield_10016",
        epic_link_field: str | None = None,
        status_map: dict[str, str] | None = None,
        client: HttpClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.project_key = project_key
        self.points_field = points_field
        self.epic_link_field = epic_link_field
        self.status_map = status_map or {}
        self._auth = (email, token)
        self._client = client
        self._categories: dict[str, str] = {}

    # ----- fetching -----

    def fetch(self, since: datetime | None) -> list[Event]:
        client = self._client or HttpClient("jira", self.base_url, auth=self._auth,
                                            headers={"Accept": "application/json"})
        try:
            self._categories = {s["name"].lower(): s.get("statusCategory", {}).get("key", "")
                                for s in client.get_json("/rest/api/3/status")}
            events: list[Event] = []
            issues = 0
            for issue in self._search(client, since):
                issues += 1
                events += self.issue_events(issue, self._histories(client, issue))
        finally:
            if self._client is None:
                client.close()
        if since is not None:
            events = [e for e in events if e.occurred_at >= to_utc(since)]
        log.info("jira %s: %d issues, %d events", self.project_key, issues, len(events))
        return events

    def jql(self, since: datetime | None) -> str:
        jql = f'project = "{self.project_key}"'
        if since is not None:
            # JQL dates are minute-precision in the API user's timezone, so look back a day;
            # events before `since` are filtered out afterwards and duplicates are ignored.
            jql += f' AND updated >= "{to_utc(since) - timedelta(days=1):%Y/%m/%d %H:%M}"'
        return jql + " ORDER BY created ASC"

    def _search(self, client: HttpClient, since: datetime | None):
        fields = ["summary", "status", "assignee", "issuetype", "parent", "duedate", "created", "reporter",
                  self.points_field, *([self.epic_link_field] if self.epic_link_field else [])]
        params: dict[str, Any] = {"jql": self.jql(since), "fields": ",".join(fields), "expand": "changelog",
                                  "maxResults": PAGE_SIZE}
        while True:
            page = client.get_json("/rest/api/3/search/jql", params)
            yield from page.get("issues", [])
            token = page.get("nextPageToken")
            if page.get("isLast", True) or not token:
                return
            params = {**params, "nextPageToken": token}

    def _histories(self, client: HttpClient, issue: dict[str, Any]) -> list[dict[str, Any]]:
        """Every changelog entry, oldest first. The search only embeds the first page."""
        changelog = issue.get("changelog") or {}
        histories = list(changelog.get("histories", []))
        if changelog.get("total", len(histories)) > len(histories):
            histories, start = [], 0
            while True:
                page = client.get_json(f"/rest/api/3/issue/{issue['key']}/changelog",
                                       {"startAt": start, "maxResults": PAGE_SIZE})
                values = page.get("values", [])
                histories += values
                start += len(values)
                if page.get("isLast", True) or not values:
                    break
        return sorted(histories, key=lambda h: _time(h["created"]))

    # ----- mapping -----

    def status(self, name: str | None) -> str | None:
        if name is None:
            return None
        if name in self.status_map:
            return self.status_map[name]
        if name in WORKFLOW:
            return name
        category = self._categories.get(name.lower())
        if category == "done":
            return "Done"
        if category == "new":
            return "To Do"
        return "In Review" if "review" in name.lower() else "In Progress"

    def _change(self, item: dict[str, Any]) -> tuple[str, str, Any, Any, dict[str, Any]] | None:
        """(event kind, state attribute, old value, new value, extra payload) for a changelog item."""
        field, field_id = (item.get("field") or "").lower(), item.get("fieldId") or ""
        old, new = item.get("fromString"), item.get("toString")
        if field_id == "status" or field == "status":
            return "status_changed", "status", self.status(old), self.status(new), {"from_name": old, "to_name": new}
        if field_id == "assignee" or field == "assignee":
            return "assignee_changed", "assignee", old, new, {}
        if field_id == "duedate" or field == "duedate":
            return ("due_date_changed", "due_date", (item.get("from") or old or "")[:10] or None,
                    (item.get("to") or new or "")[:10] or None, {})
        if field_id == "summary" or field == "summary":
            return "title_changed", "title", old, new, {}
        if field in PARENT_FIELDS or (self.epic_link_field and field_id == self.epic_link_field):
            return "parent_changed", "parent", _key(old), _key(new), {}
        if field_id == self.points_field or field in POINTS_NAMES:
            return "points_changed", "points", _points(old), _points(new), {}
        if field == "sprint":
            return "sprint_changed", "sprint", old, new, {}
        return None

    def issue_events(self, issue: dict[str, Any], histories: list[dict[str, Any]]) -> list[Event]:
        key, f = issue["key"], issue.get("fields") or {}
        url = f"{self.base_url}/browse/{key}"
        parent = (f.get("parent") or {}).get("key") or (f.get(self.epic_link_field) if self.epic_link_field else None)
        state: dict[str, Any] = {
            "title": f.get("summary") or "",
            "status": self.status((f.get("status") or {}).get("name")),
            "assignee": (f.get("assignee") or {}).get("displayName"),
            "parent": parent if isinstance(parent, str) else None,
            "points": _points(f.get(self.points_field)),
            "due_date": f.get("duedate"),
            "sprint": None,
        }
        changes: list[Event] = []
        for history in histories:
            when, actor = _time(history["created"]), (history.get("author") or {}).get("displayName")
            for item in history.get("items", []):
                change = self._change(item)
                if change is None:
                    continue
                kind, _, old, new, extra = change
                if old == new:  # e.g. two Jira statuses that map to the same workflow step
                    continue
                changes.append(Event(source="jira", kind=kind, occurred_at=when, entity_id=key, actor=actor, url=url,
                                     payload={"from": old, "to": new, **extra}))
        # Walk back from the current values to the state at creation.
        for history in reversed(histories):
            for item in reversed(history.get("items", [])):
                if change := self._change(item):
                    state[change[1]] = change[2]
        created = Event(
            source="jira", kind="ticket_created", occurred_at=_time(f["created"]), entity_id=key, url=url,
            actor=(f.get("reporter") or {}).get("displayName"),
            payload={"issue_type": (f.get("issuetype") or {}).get("name", "Task"), "title": state["title"],
                     "status": state["status"] or "To Do", "assignee": state["assignee"], "parent": state["parent"],
                     "points": state["points"], "due_date": state["due_date"]},
        )
        return [created, *changes]
