# TODO

This file tracks follow-up work that is useful, but intentionally not required for the current stable runtime.

## Process Tracking

- Add a lightweight admin review queue for notes that were auto-hidden after reports.
- Add Discord command tests that exercise the DM-first process flow end to end.
- Add slash-command parity for the main process commands once the text-command UX is stable.
- Decide when it is safe to remove the legacy company-only process tables after enough migrations have happened.

## Jobs / Sources

- Keep improving non-GitHub source freshness so the bot is less dependent on the Simplify repos.
- Harden noisy Workday and Wellfound adapters or disable them per-source when they regress.
- Add more source fixtures for format drift in ATS/company-page parsing.

## Database / Ops

- Expand Postgres coverage beyond the current smoke path into more of the process-tracking tables.
- Add a CI job that runs the Postgres-backed integration path automatically.
- Move more schema evolution into Alembic over time so runtime bootstrap stays thin.

## Discord UX

- Add richer per-role filters to `!recent` and watchlist browsing.
- Consider a staff-only moderation summary command for hidden/deleted process notes.
