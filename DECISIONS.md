# Decisions

Judgment calls made where the spec was ambiguous, or small deviations from it.

## Phase 1

1. **`project` column on events, entity versions, runs and outcomes.** The spec has
   `snapshot.build(project, as_of)` and `--project atlas`, but no project column. Added one so
   several projects can share a database. The event uniqueness key is therefore
   (`project`, `source`, `kind`, `entity_id`, `occurred_at`).
2. **Event ids are deterministic** (`evt_` + 12 hex chars of a hash of the uniqueness key),
   not autoincrement. Re-ingesting or rebuilding the DB keeps evidence ids stable, which matters
   for citations, feedback and comparing backtest runs.
3. **Entity versions are derived, not ingested.** Connectors return only events
   (`fetch(since) -> list[Event]`, as specified). Ingest folds each touched entity's full event
   history into versions (`pewa/entities.py`) and replaces that entity's versions. Events stay
   append-only; `entity_versions` is a rebuildable view, so late or out-of-order events are
   handled correctly (tested).
4. **Links are resolved twice.** `events.epic_key` is filled at ingest from the latest known
   state, as a convenient index. Snapshots ignore it and re-resolve every link from state as of
   `as_of`, since the stored column can reflect later re-parenting (tested).
5. **`since` is inclusive** (`occurred_at >= since`). De-duplication makes re-fetching the
   boundary event harmless; a strict `>` could drop events sharing the last timestamp.
6. **`--as-of DATE` means the end of that day in UTC.** All timestamps are stored as UTC; SQLite
   holds naive UTC and a column type turns them back into aware datetimes.
7. **Entity ids:** PRs are `owner/repo#number` (unique across repos); Slack messages are
   `channel:ts` (a bare `ts` is not unique across channels).
8. **Fixture format is event-level JSON**, not raw Jira/GitHub/Slack API payloads. Linking and
   entity folding still run on it; mapping raw API responses to events is the job of the
   Phase 5 connectors and will be tested there with recorded responses.
9. **Workflow** is `To Do → In Progress → In Review → Done` (`schemas.WORKFLOW`). Categories and
   "moved backwards" detection come from this order. Configurable mappings for other Jira
   workflows can come with the Jira connector.
10. **Ingest without `--fixtures`**: sources lacking credentials are skipped with a warning. If
    none have credentials, ingest falls back to the Atlas fixtures, which keeps "fixture mode is
    the default" true. Live connectors exit with a clear message until Phase 5.
11. **Schema is created on start** (`metadata.create_all`), with no migration tool yet. Every table
    in §6 plus `llm_calls` (§10.3) exists now. `runs.label` was added for comparing runs (§12).
    `assessments.detail` holds key risks, suggested actions and errors.
12. **Extra CLI command `pewa snapshot --as-of`** prints per-epic state at a past moment. It's
    the quickest way to see replay working before assessment exists.
13. **Atlas has 69 PRs** (spec: ~60). Every ticket that would plausibly have code gets a PR, plus
    a re-fix PR and three unlinked chore PRs. Uniform sizing wasn't worth distorting the stories.
14. **Names and URLs are invented.** The company is Larkspur Labs, and every URL uses the reserved
    `.example` TLD.

## Phase 2

15. **The real-model acceptance check could not run while building this phase**: there was no API
    key. The Phase 2 criterion (Epic A amber/red with a valid citation at week 6; D and E green)
    is verified in `tests/test_assess.py` with the dry-run responder, at both Mon 9 Feb and Fri
    13 Feb. The same check against the real model is `tests/test_llm_integration.py`
    (`pytest -m llm`). The real API path was smoke-tested with a deliberately invalid key: the SDK
    accepted the request and the 401 is reported cleanly.
16. **Dry-run is a heuristic responder, not canned text.** Fixed canned replies would make the
    acceptance check meaningless. Instead the dry-run "model" rates each epic from its signals
    (weighted severity: 3+ amber, 6+ red, any severity-3 strong signal at least amber) and cites
    their evidence. It runs through the same validation, retry and storage path. Tests feed
    malformed replies through `LLM(scripted=[...])`.
17. **JSON via prompt, not structured outputs.** The spec's "retry once with the validation error"
    flow is implemented over free-text JSON parsed with Pydantic. Switching to the API's structured
    outputs (`output_config.format`) would harden it further. I didn't want to ship that unverified
    with no key to test against.
18. **Refusal fallbacks are on by default** for models that support them (`fallbacks: "default"`,
    beta `server-side-fallback-2026-07-01`). A `refusal` stop reason that still comes back is
    stored as an `unknown` assessment. Rejected credentials (401/403) abort the whole run instead
    of storing one failure per epic.
19. **No sampling parameters, default thinking.** The request sends no `temperature` (rejected on
    current models) and leaves adaptive thinking and effort at the model's defaults.
    `max_tokens` is 16000 so thinking can't truncate the JSON.
20. **Done epics are not sent to the model.** They're stored as green ("Completed on ...") with
    `detail.skipped = "done"`, which saves a call per finished epic in backtests.
21. **Missing citations drop the risk, not the rating.** A key risk whose ids are all invalid is
    removed. If that leaves an amber/red rating with no risks, the reply is invalid and is retried.
22. **`burn_gap` uses the epic's own trailing throughput.** The spec says "team's trailing 4-week
    throughput". A project-wide rate would compare one epic's work with five epics' capacity, so
    the "team" is whoever completed this epic's work. The window is the last 4 weeks or since the
    epic started, whichever is shorter. A week of history is required before it fires. It fires
    when the projected finish exceeds the time left by 25% or more.
23. **Recency window for event-type signals.** `reopened`, `ci_flaky` and `blocker_language` only
    count events within `PEWA_SIGNAL_LOOKBACK_DAYS` (14). Otherwise a reopen fixed six weeks ago
    would keep counting. `due_date_pushed` and `scope_growth` look at the whole history.
24. **Signal evidence stays inside the epic.** `owner_overload` cites only the overloaded person's
    tickets in that epic (the count still covers all epics). That keeps every cited id linked to
    the epic it is cited for, which the citation-validity metric (§12) relies on.
25. **Atlas generator: ticket durations now scale with points, and Epic E is Priya's alone.**
    Phase 1 scheduled tickets evenly by count, so healthy epics burned points unevenly and
    `burn_gap` flagged Epic E, a healthy control. With durations proportional to points the
    controls burn at a steady pace. The planted stories are unchanged, and the dataset now has
    428 messages.
26. **`PEWA_ANTHROPIC_API_KEY` is accepted alongside `ANTHROPIC_API_KEY`, and wins when both are
    set.** In Claude Code cloud environments a user-defined `ANTHROPIC_API_KEY` never reached the
    container, presumably because the host reserves it for Claude Code's own login. pewa passes
    the resolved key to the SDK explicitly. With neither set, the SDK's usual lookup still applies.
