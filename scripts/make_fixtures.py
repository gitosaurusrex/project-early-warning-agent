#!/usr/bin/env python3
"""Generate the synthetic "Atlas" project used for demos and tests (SPEC §11).

Larkspur Labs is a fictional company; every person, URL and conversation here is invented.
The output is deterministic, so re-running this script reproduces the committed files exactly.

    uv run python scripts/make_fixtures.py [--out data/fixtures/atlas]

Planted stories:
  A  Payments API     PR sits 8 business days waiting on the ledger team; slips 2 weeks.
  B  Search revamp    scope grows 40% mid-flight; slips 1 week.
  C  Onboarding flow  noisy (chatter, reopened ticket, flaky CI) but ships on time.
  D  Notifications    healthy control.
  E  Admin reporting  healthy control.
"""

import argparse
import json
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

SEED = 20260105
START = date(2026, 1, 5)  # Monday of week 1
END = date(2026, 3, 13)  # Friday of week 10
PROJECT_KEY = "ATL"
API = "larkspur/atlas-api"
WEB = "larkspur/atlas-web"
CHANNELS = {"atlas-eng": "C0ATLASENG", "atlas-standup": "C0ATLASSTD", "atlas-design": "C0ATLASDSN"}

DIEGO, HANA, AMA, TOMASZ, PRIYA, LIAM = (
    "diego.ferreira",
    "hana.sato",
    "ama.owusu",
    "tomasz.wilk",
    "priya.natarajan",
    "liam.orourke",
)
ROSA, MEI, KEN, BOT, CI = "rosa.delgado", "mei.lindqvist", "ken.ibarra", "standup-bot", "github-actions"
ENGINEERS = [DIEGO, HANA, AMA, TOMASZ, PRIYA, LIAM]
PEOPLE = {
    DIEGO: {"name": "Diego Ferreira", "role": "engineer"},
    HANA: {"name": "Hana Sato", "role": "engineer"},
    AMA: {"name": "Ama Owusu", "role": "engineer"},
    TOMASZ: {"name": "Tomasz Wilk", "role": "engineer"},
    PRIYA: {"name": "Priya Natarajan", "role": "engineer"},
    LIAM: {"name": "Liam O'Rourke", "role": "engineer"},
    ROSA: {"name": "Rosa Delgado", "role": "technical program manager"},
    MEI: {"name": "Mei Lindqvist", "role": "product designer"},
    KEN: {"name": "Ken Ibarra", "role": "engineer, ledger team (another team)"},
    BOT: {"name": "Standup Bot", "role": "bot"},
}

EPICS = {
    "A": {"title": "Payments API", "owner": DIEGO, "due": date(2026, 2, 20), "actual": date(2026, 3, 6)},
    "B": {"title": "Search revamp", "owner": HANA, "due": date(2026, 3, 6), "actual": date(2026, 3, 13)},
    "C": {"title": "Onboarding flow", "owner": AMA, "due": date(2026, 2, 27), "actual": date(2026, 2, 26)},
    "D": {"title": "Notifications service", "owner": TOMASZ, "due": date(2026, 2, 13), "actual": date(2026, 2, 12)},
    "E": {"title": "Admin reporting", "owner": PRIYA, "due": date(2026, 3, 13), "actual": date(2026, 3, 11)},
}


def D(month: int, day: int) -> date:
    return date(2026, month, day)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def is_bday(day: date) -> bool:
    return day.weekday() < 5


def bdays(a: date, b: date) -> list[date]:
    return [a + timedelta(days=i) for i in range((b - a).days + 1) if is_bday(a + timedelta(days=i))]


def add_bdays(day: date, n: int) -> date:
    step = 1 if n >= 0 else -1
    while n:
        day += timedelta(days=step)
        if is_bday(day):
            n -= step
    return day


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].rstrip("-")


@dataclass
class Ticket:
    slug: str
    epic: str | None
    issue_type: str
    title: str
    assignee: str
    reporter: str
    created: datetime
    points: int | None = None
    due: date | None = None
    changes: list[tuple[datetime, str, Any, str]] = field(default_factory=list)  # when, field, to, actor
    key: str = ""


@dataclass
class PR:
    slug: str
    repo: str
    author: str
    opened: datetime
    title: str
    branch: str
    body: str
    timeline: list[tuple[datetime, str, str, dict[str, Any]]] = field(default_factory=list)  # when, kind, actor, payload
    number: int = 0


@dataclass
class Msg:
    channel: str
    when: datetime
    user: str
    text: str
    root: "Msg | None" = None
    ts: str = ""


class Atlas:
    def __init__(self) -> None:
        self.rng = random.Random(SEED)
        self.tickets: dict[str, Ticket] = {}
        self.prs: dict[str, PR] = {}
        self.msgs: list[Msg] = []
        self.work: list[tuple[str, str, date, date]] = []  # assignee, ticket slug, start, done
        self.spans: dict[str, tuple[date, date]] = {}
        self.standup_overrides: dict[tuple[str, date], str] = {}

    # ----- primitives -------------------------------------------------------------------------

    def minute(self, lo: int = 0, hi: int = 59) -> int:
        return self.rng.randint(lo, hi)

    def ticket(self, slug: str, epic: str | None, title: str, assignee: str, created: datetime, **kw: Any) -> Ticket:
        kw.setdefault("issue_type", "Story")
        kw.setdefault("reporter", assignee)
        if kw["issue_type"] != "Epic":
            kw.setdefault("points", self.rng.choices([1, 2, 3, 5], weights=[1, 3, 3, 2])[0])
        ticket = Ticket(slug=slug, epic=epic, title=title, assignee=assignee, created=created, **kw)
        self.tickets[slug] = ticket
        return ticket

    def change(self, slug: str, when: datetime, fld: str, to: Any, actor: str | None = None) -> None:
        self.tickets[slug].changes.append((when, fld, to, actor or self.tickets[slug].assignee))

    def msg(self, channel: str, when: datetime, user: str, text: str, root: Msg | None = None) -> Msg:
        assert root is None or when > root.when, f"reply before root: {text}"
        message = Msg(channel=channel, when=when, user=user, text=text, root=root)
        self.msgs.append(message)
        return message

    def thread(self, channel: str, when: datetime, user: str, text: str, replies: list[tuple[str, str]]) -> Msg:
        root = self.msg(channel, when, user, text)
        t = when
        for reply_user, reply_text in replies:
            t += timedelta(minutes=self.minute(3, 25))
            self.msg(channel, t, reply_user, reply_text, root=root)
        return root

    def reviewer_for(self, author: str) -> str:
        return self.rng.choice([e for e in ENGINEERS if e != author])

    def open_pr(self, slug: str, ticket_slug: str | None, repo: str, author: str, opened: datetime) -> PR:
        """A PR linked to its ticket via the title, branch name or body (varied on purpose)."""
        handle = author.split(".")[0]
        if ticket_slug is None:
            title = slug.split(":", 1)[1]
            pr = PR(slug, repo, author, opened, title, f"{handle}/{slugify(title)}", "Housekeeping, no ticket.")
        else:
            t = self.tickets[ticket_slug]
            style = self.rng.choice(["title", "branch", "body"])
            ref = f"{{t:{ticket_slug}}}"
            title = f"{ref}: {t.title}" if style == "title" else t.title
            branch = f"{handle}/{{tl:{ticket_slug}}}-{slugify(t.title)}" if style == "branch" else f"{handle}/{slugify(t.title)}"
            body = f"Closes {ref}.\n\nSee ticket for context." if style == "body" else "Implements the change described in the ticket."
            pr = PR(slug, repo, author, opened, title, branch, body)
        self.prs[slug] = pr
        return pr

    # ----- ticket lifecycles ------------------------------------------------------------------

    def work_ticket(
        self,
        slug: str,
        start: date,
        done: date,
        repo: str | None,
        *,
        ci_failures: int = 0,
        announce: bool | None = None,
        set_in_progress: bool = True,
        pr_slug: str | None = None,
    ) -> PR | None:
        """Normal flow: In Progress -> PR -> quick review -> merge -> Done."""
        t = self.tickets[slug]
        self.work.append((t.assignee, slug, start, done))
        self.spans[slug] = (start, done)
        if set_in_progress:
            self.change(slug, at(start, 10, self.minute()), "status", "In Progress")
        if repo is None:
            self.change(slug, at(done, 16, self.minute()), "status", "Done")
            return None

        same_day = start == done
        open_day = done if same_day else add_bdays(done, -1)
        opened = at(open_day, 13 if same_day else 14, self.minute(0, 20))
        reviewer = self.reviewer_for(t.assignee)
        reviewed = opened + timedelta(minutes=self.minute(100, 130)) if same_day else at(done, 10, self.minute())
        merged = reviewed + timedelta(minutes=self.minute(30, 60))

        pr = self.open_pr(pr_slug or f"{slug}.pr", slug, repo, t.assignee, opened)
        pr.timeline.append((opened + timedelta(minutes=2), "pr_review_requested", t.assignee, {"reviewer": reviewer}))
        for i in range(ci_failures):
            pr.timeline.append((opened + timedelta(minutes=20 + 25 * i), "ci_failed", CI, {"check": "test"}))
        pr.timeline.append((reviewed, "pr_reviewed", reviewer, {"state": "approved"}))
        pr.timeline.append((merged, "pr_merged", t.assignee, {}))
        self.change(slug, opened + timedelta(minutes=3), "status", "In Review")
        self.change(slug, merged + timedelta(minutes=4), "status", "Done")

        if announce if announce is not None else self.rng.random() < 0.35:
            root = self.msg("atlas-eng", opened + timedelta(minutes=6), t.assignee, f"PR up for {{t:{slug}}}: {{pr:{pr.slug}}}")
            if self.rng.random() < 0.6:
                self.msg("atlas-eng", reviewed - timedelta(minutes=8), reviewer, self.rng.choice(["On it 👀", "Looking now", "Will review shortly"]), root=root)
        return pr

    def plan(
        self,
        epic: str,
        assignee: str,
        items: list[tuple[str, str, bool]],
        window: tuple[date, date],
        created: datetime,
        repos: list[str],
    ) -> list[str]:
        """Create tickets and work them back to back across the window. items: (slug, title, has_pr)."""
        days = bdays(*window)
        assert len(days) >= len(items), f"window too short for {epic}/{assignee}"
        slugs = []
        for i, (slug, title, has_pr) in enumerate(items):
            full = f"{epic}.{slug}"
            self.ticket(full, epic, title, assignee, created + timedelta(minutes=2 * i))
            segment = days[i * len(days) // len(items) : (i + 1) * len(days) // len(items)]
            self.work_ticket(full, segment[0], segment[-1], self.rng.choice(repos) if has_pr else None)
            slugs.append(full)
        return slugs

    def epic(self, key: str, created: datetime, start: date) -> None:
        spec = EPICS[key]
        self.ticket(key, None, spec["title"], spec["owner"], created, issue_type="Epic", reporter=ROSA, due=spec["due"])
        self.change(key, at(start, 9, 55), "status", "In Progress")
        self.change(key, at(spec["actual"], 17, 0), "status", "Done")

    # ----- the project ------------------------------------------------------------------------

    def build(self) -> None:
        for i, key in enumerate("ABCDE"):
            self.epic(key, at(START, 7, 30 + 2 * i), {"B": D(1, 12), "E": D(1, 19)}.get(key, START))
        self.epic_a()
        self.epic_b()
        self.epic_c()
        self.epic_d()
        self.epic_e()
        self.chores()
        self.standups()
        self.chatter()

    def epic_a(self) -> None:
        """Payments API: healthy for three weeks, then stuck behind the ledger team's review."""
        backlog = at(START, 8, 0)
        self.plan("A", DIEGO, [
            ("skeleton", "Payments API skeleton and routing", True),
            ("tokenize", "Card tokenization client", True),
            ("intent", "Payment intent model", True),
            ("charge", "Charge endpoint", True),
            ("currency", "Currency handling and rounding", True),
            ("errors", "Payments error taxonomy", True),
            ("scopes", "Auth scopes for payments API", True),
        ], (D(1, 5), D(1, 23)), backlog, [API])
        self.plan("A", LIAM, [
            ("openapi", "OpenAPI spec for payments", False),
            ("sandbox", "Sandbox merchant fixtures", True),
        ], (D(1, 26), D(1, 30)), backlog + timedelta(minutes=14), [API])

        story = backlog + timedelta(minutes=18)
        self.ticket("A.ledger", "A", "Integrate ledger idempotency keys", DIEGO, story, points=5)
        self.ticket("A.refunds", "A", "Refund endpoint", DIEGO, story + timedelta(minutes=2), points=3)
        self.ticket("A.webhooks", "A", "Payment status webhooks", DIEGO, story + timedelta(minutes=4), points=3)
        self.ticket("A.payouts", "A", "Payout scheduling", DIEGO, story + timedelta(minutes=6), points=5)
        self.ticket("A.recon", "A", "Daily reconciliation job", DIEGO, story + timedelta(minutes=8), points=3)
        self.ticket("A.docs", "A", "Payments API docs", TOMASZ, story + timedelta(minutes=10), points=2)
        self.ticket("A.loadtest", "A", "Load test charge path", TOMASZ, story + timedelta(minutes=12), points=3)

        # The ledger PR: opened Fri Jan 30, first review only on Wed Feb 11 (8 business days later).
        self.work.append((DIEGO, "A.ledger", D(1, 26), D(1, 30)))
        self.spans["A.ledger"] = (D(1, 26), D(2, 18))
        self.change("A.ledger", at(D(1, 26), 10, 5), "status", "In Progress")
        pr = self.open_pr("A.ledger.pr", "A.ledger", API, DIEGO, at(D(1, 30), 14, 10))
        pr.title = "{t:A.ledger}: Integrate ledger idempotency keys"
        pr.branch = "diego/{tl:A.ledger}-ledger-idempotency"
        pr.body = "Adds idempotency keys to every ledger write. Needs sign-off from the ledger team on the key schema."
        pr.timeline += [
            (at(D(1, 30), 14, 12), "pr_review_requested", DIEGO, {"reviewer": KEN}),
            (at(D(2, 11), 11, 30), "pr_reviewed", KEN, {"state": "changes_requested"}),
            (at(D(2, 11), 16, 5), "ci_failed", CI, {"check": "integration-tests"}),
            (at(D(2, 12), 10, 15), "ci_failed", CI, {"check": "integration-tests"}),
            (at(D(2, 12), 14, 0), "pr_review_requested", DIEGO, {"reviewer": KEN}),
            (at(D(2, 17), 16, 0), "pr_reviewed", KEN, {"state": "approved"}),
            (at(D(2, 18), 10, 0), "pr_merged", DIEGO, {}),
        ]
        self.change("A.ledger", at(D(1, 30), 14, 13), "status", "In Review")
        self.change("A.ledger", at(D(2, 11), 11, 45), "status", "In Progress")
        self.change("A.ledger", at(D(2, 12), 14, 2), "status", "In Review")
        self.change("A.ledger", at(D(2, 18), 10, 4), "status", "Done")

        # Dependent work gets started, then sits idle until the ledger change lands.
        self.change("A.refunds", at(D(2, 2), 10, 20), "status", "In Progress")
        self.change("A.webhooks", at(D(2, 3), 10, 40), "status", "In Progress")
        self.change("A.payouts", at(D(2, 4), 11, 5), "status", "In Progress")
        self.work_ticket("A.refunds", D(2, 19), D(2, 24), API, set_in_progress=False, announce=True)
        self.work_ticket("A.webhooks", D(2, 25), D(2, 27), API, set_in_progress=False)
        self.work_ticket("A.payouts", D(3, 2), D(3, 3), API, set_in_progress=False, ci_failures=1)
        self.work_ticket("A.recon", D(3, 4), D(3, 5), API)
        self.work_ticket("A.docs", D(2, 23), D(2, 25), None)
        self.work_ticket("A.loadtest", D(3, 2), D(3, 5), API)

        self.change("A", at(D(2, 17), 15, 30), "due", D(3, 6), actor=ROSA)

        eng = "atlas-eng"
        self.msg(eng, at(D(1, 30), 14, 16), DIEGO, "PR for {t:A.ledger} is up: {pr:A.ledger.pr}. @ken.ibarra could the ledger team take a look? We need your sign-off on the idempotency key schema.")
        blocked = self.msg(eng, at(D(2, 2), 10, 25), DIEGO, "Blocked on the ledger team for {t:A.ledger}: still no review on {pr:A.ledger.pr}.")
        self.msg(eng, at(D(2, 2), 10, 41), ROSA, "Want me to escalate to their EM?", root=blocked)
        self.msg(eng, at(D(2, 2), 10, 52), DIEGO, "Let's give it a day or two.", root=blocked)
        self.msg(eng, at(D(2, 4), 15, 2), DIEGO, "Still waiting on ledger. Refunds and payouts both depend on the new idempotency keys, so we can't proceed on those until it lands.", root=blocked)
        self.msg(eng, at(D(2, 5), 9, 48), ROSA, "Escalated to the ledger EM, they're heads-down on quarter close.", root=blocked)
        check = self.msg(eng, at(D(2, 9), 11, 0), ROSA, "@diego.ferreira is Payments ({t:A}) still on track for Feb 20?")
        self.msg(eng, at(D(2, 9), 11, 14), DIEGO, "Honestly not sure. We're waiting on the ledger team for {t:A.ledger}, and three tickets are stuck behind it. Even if it lands Wednesday we're tight.", root=check)
        self.msg(eng, at(D(2, 9), 11, 20), ROSA, "Ok. Let's revisit Thursday.", root=check)
        self.msg(eng, at(D(2, 10), 17, 5), KEN, "Sorry all, ledger team is buried in quarter close. Will review tomorrow.", root=blocked)
        self.msg(eng, at(D(2, 11), 11, 50), DIEGO, "Ledger requested changes on {pr:A.ledger.pr}: the key schema needs a version field. Reworking now.")
        self.msg(eng, at(D(2, 13), 10, 30), DIEGO, "CI keeps failing on the ledger sandbox for {pr:A.ledger.pr}. Looking into it, re-requested review from Ken.")
        self.msg(eng, at(D(2, 17), 15, 35), ROSA, "Heads up: Payments API ({t:A}) won't make Feb 20. Moving the target to Mar 6 now that the ledger review is unblocking.")
        self.msg(eng, at(D(3, 6), 17, 10), DIEGO, "Payments API ({t:A}) is live 🎉 Thanks for the patience everyone.")

        lines = [
            "Blocked on the ledger team's review of {t:A.ledger}. Starting groundwork on refunds in the meantime.",
            "Still waiting on ledger for {t:A.ledger}. Refunds and payouts can't proceed without the new keys.",
            "No movement on the ledger review for {t:A.ledger} yet. Pinged Ken again.",
            "Waiting on the ledger team. Poking at webhooks, but most of it depends on {t:A.ledger}.",
        ]
        for i, day in enumerate(bdays(D(2, 2), D(2, 10))):
            self.standup_overrides[(DIEGO, day)] = lines[i % len(lines)]
        self.standup_overrides[(DIEGO, D(2, 11))] = "Ledger requested changes on {t:A.ledger}; reworking the key schema."
        self.standup_overrides[(DIEGO, D(2, 12))] = "Fighting CI on the ledger sandbox for {t:A.ledger}."
        self.standup_overrides[(DIEGO, D(2, 13))] = "{t:A.ledger} is back with the ledger team for re-review."
        self.standup_overrides[(DIEGO, D(2, 16))] = "Still waiting on ledger re-review of {t:A.ledger}."
        self.standup_overrides[(DIEGO, D(2, 17))] = "Ken says approval today for {t:A.ledger}. Then refunds, webhooks and payouts."

    def epic_b(self) -> None:
        """Search revamp: four tickets (+40%) added in weeks 5-6 push the date out a week."""
        backlog = at(D(1, 12), 8, 30)
        self.plan("B", HANA, [
            ("mapping", "Search index mapping v2", True),
            ("parser", "Query parser rewrite", True),
            ("relevance", "Relevance scoring tweaks", True),
            ("pagination", "Search results API pagination", True),
            ("autocomplete", "Autocomplete endpoint", True),
            ("results-ui", "Search UI: results page", True),
            ("states-ui", "Search UI: empty and error states", True),
        ], (D(1, 12), D(2, 27)), backlog, [API, WEB])
        self.plan("B", LIAM, [
            ("backfill", "Reindex backfill job", True),
            ("latency", "Search latency dashboard", False),
            ("flag", "Feature flag for new search", True),
        ], (D(2, 2), D(2, 20)), backlog + timedelta(minutes=14), [API])

        self.ticket("B.typo", "B", "Typo tolerance (fuzzy matching)", HANA, at(D(2, 4), 11, 10), points=5)
        self.ticket("B.daterange", "B", "Date-range filters", LIAM, at(D(2, 4), 11, 12), points=3)
        self.ticket("B.synonyms", "B", "Synonym dictionary admin", HANA, at(D(2, 10), 15, 20), points=3)
        self.ticket("B.analytics", "B", "Search analytics events", LIAM, at(D(2, 11), 10, 5), points=2)
        self.work_ticket("B.typo", D(3, 2), D(3, 6), API)
        self.work_ticket("B.synonyms", D(3, 9), D(3, 13), WEB)
        self.work_ticket("B.daterange", D(2, 23), D(3, 4), API)
        self.work_ticket("B.analytics", D(3, 5), D(3, 12), WEB)
        self.change("B", at(D(3, 2), 11, 0), "due", D(3, 13), actor=ROSA)

        eng = "atlas-eng"
        self.thread(eng, at(D(2, 3), 10, 30), ROSA, "Product review outcome: search revamp ({t:B}) needs typo tolerance and date-range filters before launch. @hana.sato can you scope these?", [
            (HANA, "On it. That's a decent chunk of work, adding tickets now."),
            (ROSA, "Thanks. Let's see how it affects the Mar 6 date."),
        ])
        self.msg(eng, at(D(2, 4), 11, 20), HANA, "Added {t:B.typo} and {t:B.daterange} to the search epic.")
        self.msg(eng, at(D(2, 10), 15, 30), HANA, "Also adding {t:B.synonyms}, and growth asked for {t:B.analytics} as a late add.")
        self.thread(eng, at(D(2, 24), 14, 0), HANA, "Search revamp ({t:B}) is tracking about a week behind with the added scope.", [
            (ROSA, "Understood. I'll take it to Monday's planning."),
        ])
        self.msg(eng, at(D(3, 2), 11, 5), ROSA, "Moving search revamp ({t:B}) target to Mar 13 to absorb the extra scope.")
        self.msg(eng, at(D(3, 13), 17, 15), HANA, "Search revamp ({t:B}) shipped ✅")

    def epic_c(self) -> None:
        """Onboarding flow: lots of design chatter, a reopened ticket and flaky CI, but on time."""
        backlog = at(START, 8, 40)
        self.plan("C", AMA, [
            ("wireframes", "Onboarding flow wireframes review", False),
            ("welcome", "Welcome screen", True),
            ("account", "Account details step", True),
            ("workspace", "Workspace setup step", True),
            ("progress", "Progress indicator", True),
            ("validation", "Form validation for signup", True),
            ("copy", "Onboarding copy pass", False),
            ("invite", "Invite teammates step", True),
            ("skip", "Skip and resume onboarding", True),
            ("analytics", "Onboarding analytics", True),
            ("mobile", "Mobile layout fixes", True),
            ("launch", "Onboarding launch checklist", False),
        ], (D(1, 5), D(2, 26)), backlog, [WEB])
        liam = [("api", "Onboarding API endpoints", True), ("persist", "Persist onboarding progress", True),
                ("verify", "Email verification hook", True)]
        self.plan("C", LIAM, liam, (D(1, 5), D(1, 19)), backlog + timedelta(minutes=24), [API])
        self.ticket("C.e2e", "C", "E2E tests for onboarding", LIAM, backlog + timedelta(minutes=30))
        self.work_ticket("C.e2e", D(1, 20), D(1, 23), WEB, ci_failures=2, announce=True)

        # Reopened: QA finds an edge case three business days after it was closed.
        _, closed = self.spans["C.validation"]
        reopen = add_bdays(closed, 3)
        self.change("C.validation", at(reopen, 12, 40), "status", "In Progress")
        self.work.append((AMA, "C.validation", reopen, add_bdays(reopen, 1)))
        fix = self.open_pr("C.validation.pr2", "C.validation", WEB, AMA, at(reopen, 15, 30))
        fix.title = "{t:C.validation}: accept plus-addressed emails"
        fix.timeline += [
            (at(reopen, 15, 32), "pr_review_requested", AMA, {"reviewer": LIAM}),
            (at(add_bdays(reopen, 1), 10, 10), "pr_reviewed", LIAM, {"state": "approved"}),
            (at(add_bdays(reopen, 1), 11, 0), "pr_merged", AMA, {}),
        ]
        self.change("C.validation", at(reopen, 15, 33), "status", "In Review")
        self.change("C.validation", at(add_bdays(reopen, 1), 11, 5), "status", "Done")

        s = lambda slug: self.spans[f"C.{slug}"][0]  # noqa: E731
        dsn, eng = "atlas-design", "atlas-eng"
        self.thread(dsn, at(s("wireframes"), 11, 0), MEI, "First pass of onboarding wireframes for {t:C.wireframes} is in Figma. Feedback welcome!", [
            (AMA, "Love the step layout. Can we fold workspace setup into the account step?"),
            (LIAM, "Fewer steps means fewer API calls, I'm in."),
            (MEI, "Let me try a 3-step version."),
            (ROSA, "👍"),
        ])
        self.thread(dsn, at(add_bdays(s("wireframes"), 2), 14, 0), MEI, "3-step version is up. Also exploring illustrations for the welcome screen ({t:C.welcome}).", [
            (AMA, "This is much better."),
            (LIAM, "Illustrations look great."),
            (MEI, "I'll finalize colors tomorrow."),
        ])
        self.thread(dsn, at(s("welcome"), 10, 30), AMA, "Copy debate for {t:C.welcome}: 'Welcome to Atlas' vs 'Let's get you set up'. Votes?", [
            (MEI, "Second one, warmer."),
            (LIAM, "First one is clearer imo."),
            (ROSA, "Second."),
            (HANA, "Second 🙂"),
            (LIAM, "Fine, outvoted 😄"),
            (AMA, "Going with 'Let's get you set up'."),
        ])
        self.thread(dsn, at(add_bdays(s("progress"), -1), 15, 0), MEI, "Updated progress indicator designs for {t:C.progress}.", [
            (AMA, "Can the dots be clickable?"),
            (MEI, "Only for completed steps."),
            (AMA, "Perfect."),
        ])
        self.thread(dsn, at(s("validation"), 11, 15), AMA, "Validation messages for {t:C.validation}: too harsh?", [
            (MEI, "A bit. Let's soften the email one."),
            (AMA, "Updated."),
            (LIAM, "LGTM"),
        ])
        self.thread(eng, at(reopen, 12, 45), AMA, "Heads up: QA found an edge case in {t:C.validation} (emails with plus signs). Reopening, it's a small fix.", [
            (ROSA, "Thanks for catching it."),
            (AMA, "Fix is up: {pr:C.validation.pr2}"),
        ])
        self.thread(dsn, at(s("copy"), 16, 0), MEI, "Copy pass for {t:C.copy}: shortened all the step headings.", [
            (AMA, "Love it."),
            (ROSA, "Much cleaner."),
            (LIAM, "+1"),
        ])
        invite = self.msg(dsn, at(add_bdays(s("invite"), 1), 10, 0), AMA, "Waiting on design for the final invite-step illustration ({t:C.invite}). @mei.lindqvist any ETA?")
        self.msg(dsn, at(add_bdays(s("invite"), 1), 10, 20), MEI, "Uploading this afternoon!", root=invite)
        self.msg(dsn, at(add_bdays(s("invite"), 1), 14, 10), MEI, "Illustrations are in Figma ✅", root=invite)
        self.msg(dsn, at(add_bdays(s("invite"), 1), 14, 25), AMA, "Thanks, wiring it in now.", root=invite)
        self.thread(dsn, at(s("skip"), 13, 0), AMA, "Should skipping onboarding be permanent, or should we nudge later? ({t:C.skip})", [
            (MEI, "Nudge once after 3 days, then never again."),
            (ROSA, "Agree, and let's track it in analytics."),
            (AMA, "Works for me."),
            (LIAM, "API already supports a resume flag."),
        ])
        self.thread(dsn, at(s("mobile"), 11, 0), AMA, "Mobile layout looks off on small Android screens ({t:C.mobile}).", [
            (MEI, "Screenshots?"),
            (AMA, "Posted in the thread above."),
            (MEI, "Let's reduce the illustration height below 360px."),
            (AMA, "Done, PR coming."),
        ])
        self.thread(eng, at(s("launch"), 10, 0), ROSA, "Onboarding launch readiness ({t:C.launch}): anything at risk for Friday?", [
            (AMA, "We're good, final QA tomorrow."),
            (MEI, "Design signed off ✅"),
            (LIAM, "E2E suite is green."),
        ])
        self.thread(eng, at(EPICS["C"]["actual"], 17, 10), AMA, "Onboarding flow ({t:C}) is live! 🎉", [
            (ROSA, "Great work team!"),
            (MEI, "🎉🎉"),
            (HANA, "Congrats!"),
        ])
        self.thread(dsn, at(D(1, 28), 16, 0), MEI, "Anyone have opinions on the new brand palette? Thinking of using it in onboarding.", [
            (AMA, "The teal is nice."),
            (ROSA, "As long as contrast passes, go for it."),
            (MEI, "Will check contrast ratios."),
        ])
        self.thread(dsn, at(D(2, 19), 11, 30), MEI, "Design crit moved to Thursday this week.", [(AMA, "👍")])

    def epic_d(self) -> None:
        """Notifications service: healthy control, ships a day early."""
        backlog = at(START, 9, 20)
        self.plan("D", TOMASZ, [
            ("skeleton", "Notification service skeleton", True),
            ("schema", "Event schema for notifications", True),
            ("email", "Email channel adapter", True),
            ("push", "Push channel adapter", True),
            ("prefs", "User notification preferences", True),
            ("ratelimit", "Rate limiting per user", True),
            ("digest", "Digest batching", True),
            ("retry", "Retry and dead-letter queue", True),
            ("templates", "Notification templates", True),
            ("rollout", "Rollout and monitoring", False),
        ], (D(1, 5), D(2, 12)), backlog, [API])
        self.plan("D", PRIYA, [
            ("prefs-ui", "Preferences UI", True),
            ("unsubscribe", "Unsubscribe links", True),
            ("center-ui", "Notification center UI", True),
            ("metrics", "Delivery metrics", False),
        ], (D(1, 5), D(1, 16)), backlog + timedelta(minutes=20), [WEB])
        self.msg("atlas-eng", at(EPICS["D"]["actual"], 17, 20), TOMASZ, "Notifications service ({t:D}) is done and rolled out 🎉")

    def epic_e(self) -> None:
        """Admin reporting: healthy control, ships two days early."""
        backlog = at(D(1, 16), 14, 0)
        self.plan("E", PRIYA, [
            ("model", "Reporting data model", True),
            ("shell", "Admin dashboard shell", True),
            ("usage", "Usage report", True),
            ("billing", "Billing report", True),
            ("csv", "CSV export", True),
            ("daterange", "Date range picker", True),
            ("rbac", "Role-based access for reports", True),
            ("cache", "Report caching", True),
            ("scheduled", "Scheduled email reports", True),
            ("audit", "Report audit log", True),
            ("states", "Empty states and loading", True),
            ("docs", "Admin reporting docs", False),
        ], (D(1, 19), D(3, 11)), backlog, [WEB, API])
        self.plan("E", TOMASZ, [
            ("query", "Reporting query service", True),
            ("views", "Materialized views for reports", True),
            ("perf", "Report performance tuning", False),
        ], (D(2, 16), D(3, 10)), backlog + timedelta(minutes=24), [API])
        self.msg("atlas-eng", at(EPICS["E"]["actual"], 17, 0), PRIYA, "Admin reporting ({t:E}) shipped. Thanks Tomasz for the query work!")

    def chores(self) -> None:
        for slug, author, day in [("chore:Bump dependencies", TOMASZ, D(1, 14)),
                                  ("chore:Fix lint config", HANA, D(2, 5)),
                                  ("chore:Update CODEOWNERS", PRIYA, D(2, 25))]:
            opened = at(day, 11, self.minute(0, 30))
            pr = self.open_pr(slug, None, API, author, opened)
            reviewer = self.reviewer_for(author)
            pr.timeline += [
                (opened + timedelta(minutes=1), "pr_review_requested", author, {"reviewer": reviewer}),
                (opened + timedelta(minutes=90), "pr_reviewed", reviewer, {"state": "approved"}),
                (opened + timedelta(minutes=120), "pr_merged", author, {}),
            ]

    def standups(self) -> None:
        tails = ["Should have a PR up by end of day.", "On track.", "Writing tests now.", "All good.",
                 "Pairing with {pair} after lunch.", "Mostly reviews this afternoon.", ""]
        openers = ["Today: {t} ({title}).", "Continuing on {t}.", "Wrapped up some reviews, now on {t}.",
                   "Working on {t}, {title}."]
        for day in bdays(START, END):
            root = self.msg("atlas-standup", at(day, 9, 0), BOT, f"Daily standup for {day:%a %b %d}. Reply in thread 🧵")
            when = root.when
            for user in self.rng.sample(ENGINEERS, len(ENGINEERS)):
                override = self.standup_overrides.get((user, day))
                active = [slug for (u, slug, a, b) in self.work if u == user and a <= day <= b]
                if override is None and self.rng.random() < 0.3:
                    continue  # people skip standup sometimes
                if override:
                    text = override
                elif active:
                    slug = active[0]
                    opener = self.rng.choice(openers).format(t=f"{{t:{slug}}}", title=self.tickets[slug].title.lower())
                    pair = self.rng.choice([e for e in ENGINEERS if e != user]).split(".")[0].title()
                    text = f"{opener} {self.rng.choice(tails).format(pair=pair)}".strip()
                else:
                    text = self.rng.choice(["Reviews and small fixes today.", "Picking up the next ticket from the board.",
                                            "Helping out with code reviews today."])
                when += timedelta(minutes=self.minute(2, 6))
                self.msg("atlas-standup", when, user, text, root=root)

    def chatter(self) -> None:
        """Unlinked, everyday channel noise."""
        eng = "atlas-eng"
        for week in range(10):
            monday, thursday = START + timedelta(weeks=week), START + timedelta(weeks=week, days=3)
            self.msg(eng, at(thursday, 14, 30), ROSA, "Reminder: retro at 4pm today.")
            oncall = self.thread(eng, at(monday, 9, 40), ROSA, "Who has the on-call pager this week?", [])
            self.msg(eng, at(monday, 9, 52), self.rng.choice(ENGINEERS), "Me this week.", root=oncall)
            if week % 2 == 0:
                day = add_bdays(monday, self.rng.randint(1, 3))
                self.msg(eng, at(day, 16, self.minute()), self.rng.choice([TOMASZ, PRIYA, HANA]),
                         self.rng.choice(["Deploying atlas-api to staging now.", "Staging deploy is out.",
                                          "Staging looks slow today, anyone else seeing that?"]))
            if week % 3 == 1:
                self.thread(eng, at(add_bdays(monday, 2), 11, 30), LIAM, "Lunch order going in at 12, reply with your pick 🌯",
                            [(u, self.rng.choice(["Burrito bowl", "Same as last time", "Veggie wrap please", "Pass today"]))
                             for u in self.rng.sample(ENGINEERS, 3)])

    # ----- output -----------------------------------------------------------------------------

    def assign_ids(self) -> None:
        for n, t in enumerate(sorted(self.tickets.values(), key=lambda t: (t.created, t.slug)), start=1):
            t.key = f"{PROJECT_KEY}-{n}"
        for repo in (API, WEB):
            prs = sorted((p for p in self.prs.values() if p.repo == repo), key=lambda p: (p.opened, p.slug))
            for n, pr in enumerate(prs, start=101):
                pr.number = n
        seen: Counter[str] = Counter()
        for m in sorted(self.msgs, key=lambda m: m.when):
            base = int(m.when.timestamp())
            m.ts = f"{base}.{seen[f'{m.channel}{base}']:06d}"
            seen[f"{m.channel}{base}"] += 1

    def fill(self, text: str) -> str:
        def repl(match: re.Match[str]) -> str:
            kind, slug = match.groups()
            if kind == "pr":
                return pr_url(self.prs[slug])
            key = self.tickets[slug].key
            return key.lower() if kind == "tl" else key

        return re.sub(r"\{(t|tl|pr):([\w.:-]+)\}", repl, text)

    def jira_events(self) -> list[dict[str, Any]]:
        out = []
        for t in self.tickets.values():
            url = f"https://larkspur.atlassian.example/browse/{t.key}"
            state = {"status": "To Do", "due": t.due.isoformat() if t.due else None, "assignee": t.assignee}
            out.append(event(t.created, "ticket_created", t.key, t.reporter, url, {
                "issue_type": t.issue_type,
                "title": t.title,
                "status": "To Do",
                "assignee": t.assignee,
                "parent": self.tickets[t.epic].key if t.epic else None,
                "points": t.points,
                "due_date": state["due"],
            }))
            for when, fld, to, actor in sorted(t.changes, key=lambda c: c[0]):
                assert when > t.created, f"{t.slug}: change before creation"
                to = to.isoformat() if isinstance(to, date) else to
                kind = {"status": "status_changed", "due": "due_date_changed", "assignee": "assignee_changed"}[fld]
                out.append(event(when, kind, t.key, actor, url, {"from": state[fld], "to": to}))
                state[fld] = to
        return out

    def github_events(self) -> list[dict[str, Any]]:
        out = []
        for pr in self.prs.values():
            key, url = f"{pr.repo}#{pr.number}", pr_url(pr)
            out.append(event(pr.opened, "pr_opened", key, pr.author, url, {
                "repo": pr.repo,
                "number": pr.number,
                "title": self.fill(pr.title),
                "branch": self.fill(pr.branch),
                "body": self.fill(pr.body),
            }))
            for when, kind, actor, payload in sorted(pr.timeline, key=lambda x: x[0]):
                assert when > pr.opened, f"{pr.slug}: {kind} before open"
                out.append(event(when, kind, key, actor, url, payload))
        return out

    def slack_events(self) -> list[dict[str, Any]]:
        out = []
        for m in self.msgs:
            channel = CHANNELS[m.channel]
            out.append(event(m.when, "message", f"{channel}:{m.ts}", m.user,
                             f"https://larkspur.slack.example/archives/{channel}/p{m.ts.replace('.', '')}", {
                                 "channel": channel,
                                 "channel_name": m.channel,
                                 "ts": m.ts,
                                 "thread_ts": m.root.ts if m.root else None,
                                 "text": self.fill(m.text),
                             }))
        return out

    def outcomes(self) -> list[dict[str, Any]]:
        return [
            {
                "epic_key": self.tickets[k].key,
                "title": spec["title"],
                "planned_date": spec["due"].isoformat(),
                "actual_date": spec["actual"].isoformat(),
                "slipped": spec["actual"] > spec["due"],
            }
            for k, spec in EPICS.items()
        ]

    def meta(self) -> dict[str, Any]:
        return {
            "project": "atlas",
            "name": "Atlas",
            "company": "Larkspur Labs (fictional)",
            "jira_project_key": PROJECT_KEY,
            "start": START.isoformat(),
            "end": END.isoformat(),
            "repos": [API, WEB],
            "channels": CHANNELS,
            "people": PEOPLE,
            "epics": [
                {"slug": k, "key": self.tickets[k].key, "title": s["title"], "owner": s["owner"]} for k, s in EPICS.items()
            ],
        }


def pr_url(pr: PR) -> str:
    return f"https://github.example/{pr.repo}/pull/{pr.number}"


def event(when: datetime, kind: str, entity_id: str, actor: str, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"kind": kind, "occurred_at": when.isoformat(), "entity_id": entity_id, "actor": actor, "url": url,
            "payload": payload}


def write(out: Path) -> dict[str, int]:
    atlas = Atlas()
    atlas.build()
    atlas.assign_ids()
    files = {
        "jira.json": atlas.jira_events(),
        "github.json": atlas.github_events(),
        "slack.json": atlas.slack_events(),
    }
    for records in files.values():
        records.sort(key=lambda r: (r["occurred_at"], r["kind"], r["entity_id"]))
        identities = Counter((r["kind"], r["entity_id"], r["occurred_at"]) for r in records)
        assert max(identities.values()) == 1, "duplicate event identity"
        assert all(r["occurred_at"] <= at(END, 23, 59).isoformat() for r in records), "event after END"
    files["outcomes.json"] = atlas.outcomes()
    files["meta.json"] = atlas.meta()

    out.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (out / name).write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")

    return {
        "epics": len(EPICS),
        "tickets": len(atlas.tickets),
        "prs": len(atlas.prs),
        "messages": len(atlas.msgs),
        "events": sum(len(files[f]) for f in ("jira.json", "github.json", "slack.json")),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "data/fixtures/atlas")
    args = parser.parse_args()
    stats = write(args.out)
    print(f"wrote {args.out}: " + ", ".join(f"{v} {k}" for k, v in stats.items()))


if __name__ == "__main__":
    main()
