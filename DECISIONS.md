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

15. **The real-model acceptance check ran after the phase was committed.** There was no API key
    while building Phase 2, so the criterion (Epic A amber/red with a valid citation at week 6;
    D and E green) was first verified in `tests/test_assess.py` with the dry-run responder, at
    both Mon 9 Feb and Fri 13 Feb. Once a key was available, `tests/test_llm_integration.py`
    (`pytest -m llm`) passed against `claude-sonnet-5-5`. A full week-6 run on that model rated
    A red (11 valid citations), B red, the noisy C green ("resolved noise"), and D and E green,
    for about $0.06 across four calls. B being red rather than amber, with a ~9-week pace
    projection against an actual 1-week slip, is a calibration question for the Phase 4 backtest.
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

## Phase 3

27. **Reports read stored runs; they never assess.** `pewa report --as-of DATE` uses the newest
    live run on or before that moment and says which run it used. Generating a report silently
    calling the model once per epic would be surprising, so a missing run is an error that names
    the `pewa assess` command to run.
28. **"Week over week" is the newest comparable run at least 6 days older.** Comparable means same
    project, mode and model. Six days allows for a Friday run compared with a Saturday one. The
    dashboard's trend arrow uses the newest comparable earlier run, with no minimum gap. Model
    matching was added after a real-model report called ATL-2 "amber → red" by comparing
    Sonnet's run with a dry-run run from the week before: that was a model change, not an epic
    change.
29. **"No new facts" is partly enforced in code.** Ticket keys, evidence ids and URLs in a draft
    must all appear in the model's input. Word limits are checked, and the engineering draft must
    name every epic. Dates, names and numbers in prose are only covered by the prompt; checking
    them reliably would need a claim-extraction pass. A failing draft is retried once with the
    problem, then replaced by the template, with the reason shown in the CLI and dashboard. Unlike
    assessments, reports have no `unknown` state: a usable draft always comes back.
30. **Template reports.** Dry-run and the fallback fill Jinja templates (`pewa/report_templates/`),
    which drop risks per epic (3, 2, 1, 0) until the draft fits the word limit. They pass the same
    validation as model drafts (tested).
31. **Reports are not stored.** They're cheap to regenerate from a run, and their model calls are
    logged in `llm_calls`. A `reports` table can come later if drafts need history or edits.
32. **Assessments now record the epic's owner and due date** (`assessments.detail`), for reports
    and the dashboard. Runs from before Phase 3 lack them, and both views simply omit them.
33. **Dashboard choices.** It serves one project (`PEWA_PROJECT`), binds to 127.0.0.1 and has no
    authentication (out of scope, §3). HTMX 2.0.4 loads from unpkg for in-place feedback and report
    swaps. Every form is also a normal POST with redirect, so it works offline without JavaScript.
    "Run assessment" is a plain POST because it replaces the whole page anyway. The signal
    timeline describes events from a snapshot at the run's `as_of`, so titles are what was known
    then. The backtest page arrives with Phase 4.
34. **Fixture evidence links open a local event view.** The spec allows "a local view in fixture
    mode". The rule is by URL rather than by mode: hosts under the reserved `.example` TLD go to
    `/events/{id}`, and real hosts open the source.
35. **Real-model check of reports** (Sonnet 5.5, Atlas week 6). The exec draft passed on the first
    attempt at 183 words. The engineering draft came back at 663 words, was rejected for the
    600-word limit, and the retry passed at 565. About $0.15 for both.

## Phase 4

36. **Backtests are grouped with two new tables, not a new column on `runs`.** `backtests` holds the
    parameters plus the model-call count and cost, and `backtest_steps` links each step's run.
    Tables are created on start with no migration tool (#11). Adding a column to `runs` would break
    existing databases, while new tables just appear.
37. **"Flagged" means amber or red strictly before the planned date.** The spec says "at any point",
    but an epic still open after its due date is trivially late. Counting those ratings would
    turn every slip into a hit and inflate recall. Lead time uses the same first flag. Epics with
    no outcome row are shown but not scored.
38. **Citation validity counts dropped ids.** The validator removes made-up evidence ids before
    storing (#21), so stored citations alone would always score 100%. The denominator therefore adds
    the dropped ids recorded on each assessment. "Linked to that epic" is checked against a
    snapshot at the step's `as_of`, the same rule the assessment itself used.
39. **Metrics are recomputed, not stored.** Snapshots take about 0.03 s, so re-scoring a 10-step backtest
    is quick. Feedback given later counts, and the scoring rules can change without re-running
    the model. Only cost and call counts are stored, because they can't be recovered later.
40. **Steps are `--from`, `--from + step`, ... up to `--to`.** For the spec's example
    (5 Jan to 13 Mar, 7d) that is ten Mondays ending 9 Mar. Steps are not aligned to Fridays or
    to `--to`, which keeps every step the same distance apart. The CLI commits after each step, so
    a failure partway through keeps the finished steps.
41. **Chart.** One row per epic and one cell per step, using the status colors from the data-viz
    guide (green/amber/red). Each cell also carries a letter, so the rating never depends on color
    alone. A neutral ✓ marks a completed epic, so "done" isn't confused with "assessed green".
    Dashed and solid lines mark the planned and actual dates on the same time scale. Hovering a
    cell shows its rationale, and every page has a table view. The same Jinja macros
    (`pewa/report_templates/_bt_macros.html`) render the standalone HTML report and the dashboard.
42. **Comparison works across step schedules.** Two backtests with different steps still get
    side-by-side metrics and per-epic verdicts, with a note that their weeks don't line up.
43. **Backtests run from the CLI only.** About 40 model calls take several minutes, which is too
    long for a request. The dashboard lists, shows and compares them.
44. **Sustained lead time is reported alongside lead time.** The real-model run flagged ATL-2 amber
    on 19 Jan by extrapolating one week of throughput (confidence 0.55). It then rated it green
    on 2 Feb, before the planted scope growth turned it red on 9 Feb. "First flag" credits that
    as 46 days of warning, which overstates what a TPM would have acted on. Sustained lead time
    starts from the amber/red streak still running at the last step before the due date. It is
    25 days for ATL-2.
45. **First real-model backtest** (Sonnet 5.5, Atlas, 10 Mondays from 5 Jan to 9 Mar, 43 calls,
    $0.62). Recall was 100%, and both slips were flagged before their due dates (A on 2 Feb,
    18 days early; B sustained from 9 Feb, 25 days early). Precision was 67%: ATL-5 was rated amber
    once on 26 Jan, at confidence 0.45, from one finished ticket and no detector signals. The model
    said the evidence was thin but still chose amber. Citation validity was 100% (108/108), with no
    failed assessments. The dry-run baseline scores 100% precision and recall but with shorter lead
    times. It was tuned on this dataset, so it isn't a fair competitor. Both early misfires were
    low-confidence pace extrapolations from a week or two of history. That makes them the first
    target for prompt tuning (a stretch item), or for a minimum-history rule like the one
    `burn_gap` already uses.
