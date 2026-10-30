# Project Early-Warning Agent — Build Spec

## 1. What we're building

A tool for technical project managers that watches a project's Jira, GitHub and Slack activity, flags which epics are at risk of slipping, explains **why** with linked evidence, and drafts weekly status updates.

It must also be able to **replay history**: run as if it were any past date, using only data that existed then. That lets us backtest it on a finished project and show it would have flagged real slips early.

Target: a working, demo-ready MVP that runs fully offline on a bundled synthetic dataset, and against real Jira/GitHub/Slack when credentials are provided.

## 2. Core use cases

1. **Daily risk check**: TPM opens the dashboard and sees every epic rated green / amber / red with a 1–3 sentence rationale and clickable evidence.
2. **Drill-down**: TPM clicks a flagged epic and sees the timeline of signals that drove the rating.
3. **Status report**: TPM clicks "Generate weekly update" and gets two drafts: an exec summary and an engineering-detail version, both in Markdown.
4. **Feedback**: TPM marks each flag as *useful* or *noise*; this is stored and used in evals.
5. **Backtest**: TPM picks a past project and a date range; the tool replays week by week and reports how early it flagged epics that actually slipped, plus precision/recall.

## 3. Scope

**In MVP**
- Connectors: Jira Cloud (one project), GitHub (one or more repos), Slack (one or more channels). All read-only.
- Fixture mode with a bundled synthetic project (see §11) — the default, so everything works with no credentials.
- Time-aware snapshot store with `as_of` replay.
- Heuristic signal extraction + LLM risk assessment with citations.
- Weekly status report generation.
- Feedback capture.
- Backtest + eval report.
- CLI and a simple local web dashboard.

**Out of scope for MVP** (leave clean extension points)
- Posting anything to Slack/Jira (the agent never writes to external systems in MVP).
- Multi-user auth, hosting, multi-tenant support.
- Real-time streaming; nightly/on-demand batch runs are fine.
- Agentic follow-ups (asking owners questions in Slack) — see Phase 5 stretch.

## 4. Tech stack

- **Python 3.12**, managed with `uv`.
- **SQLite** via SQLAlchemy 2.x for storage.
- **Anthropic Python SDK** for LLM calls. Model configurable via env var `PEWA_MODEL`, default `claude-sonnet-5-5`.
- **httpx** for Jira/GitHub/Slack REST calls (no heavy vendor SDKs needed).
- **Pydantic v2** for all data models and LLM output validation.
- **Typer** for the CLI.
- **FastAPI + Jinja2 + HTMX** for the dashboard (server-rendered, no JS build step).
- **pytest** for tests; **respx** to mock HTTP.

## 5. Repository layout

```
pewa/
  config.py            # settings from env / .env
  db.py                # engine, session, migrations-on-start
  models.py            # SQLAlchemy tables
  schemas.py           # Pydantic models (events, signals, assessments, reports)
  connectors/
    base.py            # Connector protocol: fetch(since) -> list[Event]
    jira.py
    github.py
    slack.py
    fixtures.py        # loads synthetic dataset
  ingest.py            # runs connectors, writes events
  snapshot.py          # build ProjectState as_of a timestamp
  signals.py           # heuristic detectors
  assess.py            # LLM risk assessment per epic
  report.py            # status report generation
  backtest.py          # replay + metrics
  llm.py               # thin Anthropic client wrapper, retries, JSON validation
  prompts/             # prompt templates as .md/.j2 files
  cli.py
  web/
    app.py
    templates/
data/
  fixtures/atlas/      # synthetic project (see §11)
tests/
SPEC.md
README.md
.env.example
```

## 6. Data model

Store raw history as **append-only events** so any past state can be reconstructed. Never overwrite.

**events**
- `id`, `source` (jira|github|slack), `kind` (e.g. `ticket_created`, `status_changed`, `due_date_changed`, `pr_opened`, `pr_review_requested`, `pr_reviewed`, `pr_merged`, `ci_failed`, `message`), `occurred_at` (UTC), `entity_id` (ticket key / PR number / message ts), `epic_key` (nullable, resolved link), `actor`, `payload` (JSON), `url`.
- Unique on (`source`, `kind`, `entity_id`, `occurred_at`) to make ingestion idempotent.

**entities** — current-known metadata for tickets, epics, PRs (title, owner, links between them). Also versioned by `valid_from` so snapshots stay honest.

**runs** — each assessment run: `id`, `as_of`, `mode` (live|backtest), `model`, `created_at`.

**assessments** — per run per epic: `rating` (green|amber|red), `confidence` (0–1), `rationale`, `predicted_slip_days` (nullable), `evidence` (JSON list of event ids + urls), `signals` (JSON).

**feedback** — `assessment_id`, `verdict` (useful|noise), `note`, `created_at`.

**outcomes** — ground truth for backtests: `epic_key`, `planned_date`, `actual_date`, `slipped` (bool).

### Linking rules
- Jira: epic ↔ child tickets via parent field.
- GitHub → Jira: ticket keys (e.g. `ATL-123`) in PR title, branch name or body.
- Slack → Jira/GitHub: ticket keys or PR URLs in message text; messages in a thread inherit links from the thread root.
- Unlinked Slack messages are kept but only used as context for the channel, not attributed to an epic.

## 7. Connectors

Each connector implements `fetch(since: datetime) -> list[Event]` and must emit **historical** events, not just current state:
- **Jira**: issues + changelog (`expand=changelog`) to recover status, assignee, due date and sprint changes over time. Auth: email + API token.
- **GitHub**: PRs with timeline events (opened, review requested, reviewed, merged, closed), plus check-run failures on PRs. Auth: fine-grained read-only token.
- **Slack**: `conversations.history` and `conversations.replies` for configured channels. Auth: bot token with read scopes only.

Requirements: pagination, rate-limit handling with backoff, incremental sync from the last ingested timestamp, and tokens only from env vars. Never log tokens or full message bodies at INFO level.

## 8. Snapshots and replay (critical)

`snapshot.build(project, as_of)` returns a `ProjectState` using **only events with `occurred_at <= as_of`** and entity versions valid at that time.

This is the core integrity guarantee for backtesting. Add tests that prove no future data leaks into a snapshot (e.g. a ticket closed after `as_of` must appear open).

## 9. Signals (heuristics)

Cheap, deterministic detectors run first; their output feeds the LLM. Each signal has `type`, `severity` (1–3), `epic_key`, `evidence` (event ids), `detail`. Thresholds live in config.

Initial set:
- **stale_review**: PR awaiting review > 3 business days.
- **stale_ticket**: in-progress ticket with no linked activity > 5 business days.
- **reopened**: ticket reopened or moved backwards in workflow.
- **due_date_pushed**: due date moved later (count and total days).
- **scope_growth**: child tickets added to epic after it started (> 20% growth).
- **ci_flaky**: repeated CI failures on the same PR.
- **burn_gap**: remaining open points vs. time to due date, compared to team's trailing 4-week throughput.
- **owner_overload**: one assignee owns > N in-progress tickets across epics.
- **blocker_language**: Slack messages linked to the epic containing blocker phrases ("blocked on", "waiting on", "can't proceed", etc.) — simple keyword pass; the LLM interprets them properly.

## 10. LLM layer

### 10.1 Risk assessment (`assess.py`)
One call per epic per run. Input:
- Epic metadata (title, owner, planned date, status).
- Child ticket summary at `as_of`.
- Signals from §9.
- Up to ~40 most relevant recent events (prioritize those tied to signals), each with an **evidence id**.
- Linked Slack excerpts (trimmed).

Output must be strict JSON, validated with Pydantic:

```json
{
  "rating": "green | amber | red",
  "confidence": 0.0,
  "rationale": "1-3 sentences, plain language",
  "predicted_slip_days": null,
  "key_risks": [
    {"summary": "string", "evidence_ids": ["evt_123", "evt_456"]}
  ],
  "suggested_actions": ["string"]
}
```

Rules enforced in code, not just the prompt:
- Every `evidence_id` must exist in the supplied input; drop or reject any that don't (no hallucinated citations).
- Red/amber ratings must cite at least one evidence id.
- On invalid JSON, retry once with the validation error; on second failure, store a `rating: "unknown"` assessment with the error.

Prompt guidance: be calibrated, prefer "green" when evidence is thin, distinguish facts from inferences, and never invent dates or people.

### 10.2 Status reports (`report.py`)
Input: the latest run's assessments plus week-over-week changes. Output Markdown in two variants:
- **Exec**: overall RAG, top 3 risks, decisions needed, ≤ 200 words.
- **Engineering**: per-epic detail with links, ≤ 600 words.

Every claim about a specific epic must come from the assessments; no new facts.

### 10.3 LLM wrapper (`llm.py`)
Retries with backoff, request/response logging to a local `llm_calls` table (prompt hash, tokens, latency, cost estimate), and a `--dry-run` mode that returns canned responses for tests.

## 11. Synthetic demo dataset ("Atlas")

Generate a fictional project so the demo works with zero credentials. Put a generator script in `scripts/make_fixtures.py` and commit its output to `data/fixtures/atlas/`.

- Fictional company and team (6 engineers, invented names), 10-week timeline, 5 epics, ~80 tickets, ~60 PRs, ~400 Slack messages.
- Plant clear stories:
  - **Epic A (Payments API)**: on track until week 4, then a PR sits in review 8 days while Slack shows the owner waiting on another team; slips 2 weeks. The agent should flag it ≥ 1 week before the original due date.
  - **Epic B (Search revamp)**: scope grows 40% mid-flight; slips 1 week.
  - **Epic C (Onboarding flow)**: looks noisy (lots of chatter, a reopened ticket) but ships on time — tests false positives.
  - **Epic D, E**: healthy controls.
- Include `outcomes.json` with ground truth.

## 12. Backtesting and evals (`backtest.py`)

`pewa backtest --project atlas --from 2026-01-05 --to 2026-03-13 --step 7d`

- For each step, build snapshot, run signals + assessment, store as a `backtest` run.
- Metrics against `outcomes`:
  - **Lead time**: days between first amber/red flag and the original planned date, for epics that slipped.
  - **Precision / recall** of "red or amber at any point" vs. "actually slipped".
  - **False alarm rate** on epics that shipped on time.
  - **Citation validity**: % of cited evidence ids that exist and are linked to that epic.
- Write a Markdown + HTML report with a per-epic timeline chart (rating per week vs. outcome).
- Support comparing two runs (e.g. different prompts or models) side by side.

## 13. Interfaces

### CLI
```
pewa init                       # create db, write .env from .env.example
pewa ingest [--source all|jira|github|slack] [--fixtures atlas]
pewa assess [--as-of DATE]      # run signals + LLM, print table
pewa report [--as-of DATE] [--variant exec|eng|both] [--out FILE]
pewa backtest ...               # see §12
pewa serve                      # start dashboard on localhost:8000
```

### Dashboard
- **Overview**: table of epics with rating badges, confidence, one-line rationale, trend arrow vs. last run, date picker for `as_of`.
- **Epic detail**: rationale, key risks with evidence links (open the Jira/GitHub/Slack URL, or a local view in fixture mode), signal timeline, 👍/👎 feedback buttons with optional note.
- **Reports**: generate and copy exec/eng drafts.
- **Backtest**: run list, metrics, per-epic timeline chart.
- Clean, minimal styling; works in light and dark mode.

## 14. Configuration

`.env.example` must list: `ANTHROPIC_API_KEY`, `PEWA_MODEL`, `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY`, `GITHUB_TOKEN`, `GITHUB_REPOS` (comma-separated), `SLACK_BOT_TOKEN`, `SLACK_CHANNELS`, `PEWA_DB_PATH`, signal thresholds.

If a source's credentials are missing, skip it with a clear warning rather than failing.

## 15. Testing requirements

- Unit tests for every signal detector using small hand-built event lists.
- Snapshot leakage tests (§8).
- Connector tests with recorded/mocked HTTP responses (respx), including pagination and 429 handling.
- LLM output validation tests: invalid JSON, hallucinated evidence ids, missing citations on red.
- End-to-end test: ingest Atlas fixtures → assess with `--dry-run` LLM → report generated.
- One opt-in integration test (marked `@pytest.mark.llm`) that runs the real model on Atlas and asserts Epic A is flagged amber/red at least one week before its planned date.

## 16. Build phases

Build in order. Each phase must pass its acceptance criteria and tests before starting the next. Commit at the end of each phase.

**Phase 1 — Foundation and fixtures**
- Project scaffold, config, DB, models, Atlas generator and fixture connector, `ingest`, `snapshot`.
- ✅ `pewa ingest --fixtures atlas` loads data; snapshot leakage tests pass.

**Phase 2 — Signals and assessment**
- All §9 detectors, LLM wrapper, assessment with validation, `pewa assess`.
- ✅ On Atlas at week 6, Epic A is amber or red with ≥ 1 valid citation; Epics D/E are green.

**Phase 3 — Reports and dashboard**
- Status reports, FastAPI dashboard, feedback capture.
- ✅ `pewa serve` shows overview + epic detail; feedback persists; reports render.

**Phase 4 — Backtesting**
- Replay, metrics, report, run comparison.
- ✅ Atlas backtest produces lead-time, precision/recall and a timeline chart.

**Phase 5 — Real connectors**
- Jira, GitHub, Slack connectors with incremental sync.
- ✅ Mocked-HTTP tests pass; `README` documents token scopes and setup.

**Stretch (only if asked)**
- Draft-only clarifying questions to epic owners (shown in the dashboard for the TPM to send manually).
- Prompt tuning using collected feedback.
- Scheduled nightly runs.

## 17. Working agreements for the builder

- Read this whole spec before writing code. If something is ambiguous, pick the simplest reasonable option, note it in `DECISIONS.md`, and keep going.
- Keep the agent **read-only** against external systems.
- Keep prompts in `pewa/prompts/` as files, not inline strings.
- Prefer small, well-named modules over clever abstractions.
- Write the `README` as you go: setup, running the Atlas demo in under 5 minutes, and connecting real sources.
- After each phase, summarize what was built, what's tested, and any deviations from this spec.
