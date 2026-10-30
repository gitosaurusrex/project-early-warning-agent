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
