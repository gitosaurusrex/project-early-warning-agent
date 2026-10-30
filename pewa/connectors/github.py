"""GitHub connector (SPEC §7): pull requests, their timelines, and failed check runs, as events.

For each configured repo, PRs are listed newest-updated first, stopping at the first one not
updated since the last sync. Each PR yields `pr_opened`, then one event per timeline entry
(`pr_review_requested`, `pr_reviewed`, `pr_merged`, `pr_closed`, `pr_reopened`), plus
`ci_failed` for every failed or timed-out check run on the PR's commits.
"""

import logging
from datetime import datetime
from typing import Any

from pewa.connectors.http import HttpClient
from pewa.schemas import Event, to_utc

log = logging.getLogger(__name__)

PAGE_SIZE = 100
FAILED = {"failure", "timed_out"}
API_VERSION = "2022-11-28"


def _time(value: str) -> datetime:
    return to_utc(datetime.fromisoformat(value))


def _login(obj: dict[str, Any] | None) -> str | None:
    return (obj or {}).get("login")


class GitHubConnector:
    source = "github"

    def __init__(
        self,
        token: str,
        repos: list[str],
        *,
        api_url: str = "https://api.github.com",
        max_commits: int = 30,
        client: HttpClient | None = None,
    ):
        self.repos = repos
        self.api_url = api_url
        self.max_commits = max_commits  # check runs are read for the PR's last N commits
        self._token = token
        self._client = client

    def fetch(self, since: datetime | None) -> list[Event]:
        client = self._client or HttpClient("github", self.api_url, headers={
            "Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        })
        events: list[Event] = []
        try:
            for repo in self.repos:
                prs = list(self._pulls(client, repo, since))
                for pr in prs:
                    events += self.pr_events(client, repo, pr)
                log.info("github %s: %d PRs updated", repo, len(prs))
        finally:
            if self._client is None:
                client.close()
        if since is not None:
            events = [e for e in events if e.occurred_at >= to_utc(since)]
        return events

    def _pages(self, client: HttpClient, path: str, params: dict[str, Any] | None = None):
        """Follow Link: rel="next" headers, yielding each page's items."""
        url: str | None = path
        while url:
            response = client.get(url, params)
            yield response.json()
            url, params = response.links.get("next", {}).get("url"), None  # the next URL carries its params

    def _pulls(self, client: HttpClient, repo: str, since: datetime | None):
        params = {"state": "all", "sort": "updated", "direction": "desc", "per_page": PAGE_SIZE}
        for page in self._pages(client, f"/repos/{repo}/pulls", params):
            for pr in page:
                if since is not None and _time(pr["updated_at"]) < to_utc(since):
                    return  # sorted by update time: everything after this is older
                yield pr

    def pr_events(self, client: HttpClient, repo: str, pr: dict[str, Any]) -> list[Event]:
        number = pr["number"]
        key, url = f"{repo}#{number}", pr.get("html_url")

        def event(kind: str, when: str, actor: str | None, **payload: Any) -> Event:
            return Event(source="github", kind=kind, occurred_at=_time(when), entity_id=key, actor=actor, url=url,
                         payload=payload)

        events = [event("pr_opened", pr["created_at"], _login(pr.get("user")), repo=repo, number=number,
                        title=pr.get("title") or "", branch=(pr.get("head") or {}).get("ref"), body=pr.get("body") or "")]
        merged = bool(pr.get("merged_at"))
        for page in self._pages(client, f"/repos/{repo}/issues/{number}/timeline", {"per_page": PAGE_SIZE}):
            for item in page:
                match item.get("event"):
                    case "review_requested":
                        reviewer = _login(item.get("requested_reviewer")) or (item.get("requested_team") or {}).get("slug")
                        events.append(event("pr_review_requested", item["created_at"], _login(item.get("actor")),
                                            reviewer=reviewer))
                    case "reviewed" if item.get("submitted_at"):
                        events.append(event("pr_reviewed", item["submitted_at"], _login(item.get("user")),
                                            state=(item.get("state") or "").lower()))
                    case "merged":
                        events.append(event("pr_merged", item["created_at"], _login(item.get("actor"))))
                    case "closed" if not merged:  # a merge also emits "closed"; the merge says it all
                        events.append(event("pr_closed", item["created_at"], _login(item.get("actor"))))
                    case "reopened":
                        events.append(event("pr_reopened", item["created_at"], _login(item.get("actor"))))
        events += self._ci_failures(client, repo, number, event)
        return events

    def _ci_failures(self, client: HttpClient, repo: str, number: int, event) -> list[Event]:
        commits = [c for page in self._pages(client, f"/repos/{repo}/pulls/{number}/commits", {"per_page": PAGE_SIZE})
                   for c in page]
        failures = []
        for commit in commits[-self.max_commits:]:
            sha = commit["sha"]
            for page in self._pages(client, f"/repos/{repo}/commits/{sha}/check-runs", {"per_page": PAGE_SIZE}):
                for run in page.get("check_runs", []):
                    if run.get("conclusion") in FAILED and run.get("completed_at"):
                        app = (run.get("app") or {}).get("slug")
                        failures.append(event("ci_failed", run["completed_at"], app, check=run.get("name"),
                                              sha=sha[:12]))
        return failures
