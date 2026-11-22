# Next steps

Where ATLAS stands and what's left. Updated 22 Nov 2026. Detailed tasks are GitHub issues; this page
is the overview and a suggested order.

## Where things stand

- **Built:** all five phases of [SPEC.md](SPEC.md) (ingest and as-of snapshots, signals and LLM
  assessment, reports and dashboard, backtesting, live Jira/GitHub/Slack connectors), plus the
  thin-history guard, structured outputs, the redesigned dashboard with light/dark/auto themes, and
  the ATLAS branding. Judgment calls are recorded in [DECISIONS.md](DECISIONS.md).
- **Tested:** about 194 tests run in CI on every pull request and push to `main`. They need no
  secrets: connector tests mock all HTTP, and the real-model test (`pytest -m llm`) is opt-in.
- **Live demo:** https://mvp-atlas-demo.onrender.com, on Render's free plan, deployed by
  `render.yaml` from `main`. Every merge redeploys it. It uses the fictional Atlas data and never
  calls the model. The password is `PEWA_DASHBOARD_PASSWORD` in the Render service's
  **Environment** tab. The free instance sleeps when idle (about a minute to wake), and feedback
  added on the site resets on restart.
- **How changes ship:** a branch, then a pull request, CI, merge to `main`, and Render redeploys
  automatically.

## Open work

| Issue | What | Needs |
|---|---|---|
| [#10](https://github.com/gitosaurusrex/project-early-warning-agent/issues/10) | Validate on a real, finished project | Read-only Jira/GitHub/Slack tokens, an Anthropic key, about $1 per backtest |
| [#11](https://github.com/gitosaurusrex/project-early-warning-agent/issues/11) | Confirm connector assumptions against live APIs | Same access as #10 |
| [#12](https://github.com/gitosaurusrex/project-early-warning-agent/issues/12) | Add database migrations before the next schema change | Nothing external |
| [#13](https://github.com/gitosaurusrex/project-early-warning-agent/issues/13) | Hosted deployment on real project data | #12; a paid Render plan; a decision on storing Slack text |
| [#14](https://github.com/gitosaurusrex/project-early-warning-agent/issues/14) | Reduce model cost and runtime | An Anthropic key for measurements |
| [#15](https://github.com/gitosaurusrex/project-early-warning-agent/issues/15) | Make the evaluation more trustworthy (variance, holdout data) | An Anthropic key |
| [#16](https://github.com/gitosaurusrex/project-early-warning-agent/issues/16) | Match people across Jira, GitHub and Slack | Nothing external |
| [#17](https://github.com/gitosaurusrex/project-early-warning-agent/issues/17) | Check dates, names and numbers in report drafts | Nothing external |
| [#18](https://github.com/gitosaurusrex/project-early-warning-agent/issues/18) | Finish the rename to ATLAS (CLI, package, settings, demo dataset) | A decision on how far to rename |
| [#19](https://github.com/gitosaurusrex/project-early-warning-agent/issues/19) | Add linting and type checks to CI | Nothing external |

**Suggested order:** #10 with #11 first, because a real project is the result that matters and it
may reshape everything else. Then #12 before any schema change or real-data hosting (#13). #19 and
#17 are small and can slot in anytime. #14 and #15 pay off once real backtests run regularly.

## Other notes

- In Claude Code cloud sessions, the Anthropic key is read from `PEWA_ANTHROPIC_API_KEY`, because a
  user-defined `ANTHROPIC_API_KEY` didn't reach the container (DECISIONS #26).
- No Render, Vercel or Clerk access was configured for the cloud sessions that built this, so
  Render setup steps were done by hand in the Render dashboard.
