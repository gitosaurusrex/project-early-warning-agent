"""Linking rules (SPEC §6): attach GitHub and Slack activity to Jira tickets and epics."""

import re
from collections.abc import Iterable, Mapping
from typing import Any

from pewa.schemas import Event

TICKET_KEY_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]+-\d+)\b")
PR_URL_RE = re.compile(r"https?://[^\s/<>|]+/([\w.-]+/[\w.-]+)/pull/(\d+)")
MAX_PARENT_DEPTH = 3


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def ticket_keys(*texts: str | None) -> list[str]:
    """Ticket-key-shaped tokens, upper-cased. Callers filter against known tickets."""
    return _unique(m.upper() for text in texts if text for m in TICKET_KEY_RE.findall(text))


def pr_keys(text: str | None) -> list[str]:
    """PR keys ("owner/repo#123") for every pull-request URL in the text."""
    return _unique(f"{repo}#{number}" for repo, number in PR_URL_RE.findall(text or ""))


def pr_refs(title: str | None, branch: str | None, body: str | None) -> list[str]:
    return ticket_keys(title, branch, body)


def thread_root_id(event: Event) -> str | None:
    """Entity id of the thread root for a Slack reply, or None for top-level messages."""
    payload = event.payload
    thread_ts = payload.get("thread_ts")
    if not thread_ts or thread_ts == payload.get("ts"):
        return None
    return f"{payload.get('channel')}:{thread_ts}"


class LinkResolver:
    """Resolves events to epics given ticket and PR state (as of some moment)."""

    def __init__(self, tickets: Mapping[str, Mapping[str, Any]], prs: Mapping[str, Mapping[str, Any]]):
        self.tickets = tickets
        self.prs = prs

    def epic_for_ticket(self, key: str | None) -> str | None:
        for _ in range(MAX_PARENT_DEPTH + 1):
            ticket = self.tickets.get(key) if key else None
            if ticket is None:
                return None
            if ticket.get("issue_type") == "Epic":
                return key
            key = ticket.get("parent")
        return None

    def epic_for_refs(self, tickets: Iterable[str], prs: Iterable[str] = ()) -> str | None:
        for key in tickets:
            if epic := self.epic_for_ticket(key):
                return epic
        for pr in prs:
            if epic := self.epic_for_pr(pr):
                return epic
        return None

    def epic_for_pr(self, pr_key: str) -> str | None:
        pr = self.prs.get(pr_key)
        return self.epic_for_refs(pr.get("refs", [])) if pr else None

    def resolve(self, events: Iterable[Event]) -> dict[str, str | None]:
        """Map event id -> epic key. Events must be in time order so thread roots come first."""
        result: dict[str, str | None] = {}
        thread_refs: dict[str, tuple[list[str], list[str]]] = {}
        for event in events:
            assert event.id is not None, "events must have ids before linking"
            if event.source == "jira":
                result[event.id] = self.epic_for_ticket(event.entity_id)
            elif event.source == "github":
                result[event.id] = self.epic_for_pr(event.entity_id)
            else:
                text = event.payload.get("text")
                tickets, prs = ticket_keys(text), pr_keys(text)
                root = thread_root_id(event)
                if root is None:
                    thread_refs[event.entity_id] = (tickets, prs)
                elif root in thread_refs:
                    root_tickets, root_prs = thread_refs[root]
                    tickets, prs = _unique([*tickets, *root_tickets]), _unique([*prs, *root_prs])
                result[event.id] = self.epic_for_refs(tickets, prs)
        return result

    def ticket_refs(self, event: Event) -> list[str]:
        """Known non-epic tickets an event touches directly (used for per-ticket activity)."""
        if event.source == "jira":
            refs = [event.entity_id]
        elif event.source == "github":
            refs = list(self.prs.get(event.entity_id, {}).get("refs", []))
        else:
            text = event.payload.get("text")
            refs = ticket_keys(text)
            for pr in pr_keys(text):
                refs += self.prs.get(pr, {}).get("refs", [])
        return [k for k in _unique(refs) if k in self.tickets and self.tickets[k].get("issue_type") != "Epic"]
