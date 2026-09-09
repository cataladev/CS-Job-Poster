# Contributing

This project publishes curated U.S. internships and new-grad roles to two primary
Discord channels, with optional category-thread mirrors. Read `README.md` before
changing runtime behavior.

## Setup

Use Python 3.11 and Postgres for application runs:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp deploy/dev.env.example .env
make postgres-up
make db-migrate ALEMBIC=".venv/bin/python -m alembic"
make run-postgres
```

SQLite is supported for isolated unit tests only.
Start Docker Desktop before the Make targets and configure the development `.env`
before starting the bot. For an existing setup, the combined command is
`make dev-postgres ALEMBIC=".venv/bin/python -m alembic"`. The Alembic override avoids
stale executable paths after a checkout moves.

## Working agreement

- Keep source-specific parsing in `job_bot/github_feeds.py` and use typed
  `JobObservation` values at the shared boundary.
- Treat duplicate prevention as a correctness invariant. New keys may merge existing
  canonicals, so every change needs positive and negative merge cases.
- Preserve the silent-baseline rule: incomplete initial snapshots must never publish.
- Treat malformed or suspiciously empty feeds as unavailable, not as evidence that
  jobs disappeared.
- Keep Discord sending behind the transactional outbox and marker reconciliation.
- Keep optional category mirrors downstream of successful primary delivery. Preserve
  primary company/location pings and Daily Brief role pings; copies must not ping again.
- Initialize every permanent category card and thread at startup in `CATEGORY_ORDER`,
  with SWE first and OTHER last, independently of incoming listings. Cards and
  threads use only the category name and persist across days. Reuse stable
  bot-authored markers across restarts and interrupted creation without limiting
  history by date. Keep older dated cards separate and preserve existing threads.
- Classify roles at the mirror boundary, without narrowing upstream API requests
  for presentation. Unknown roles belong in OTHER. Daily Briefs remain only in the
  primary channels, never inside category threads.
- Never weaken safe-link validation to rescue a single malformed upstream row.
- Treat U.S.-only delivery as a hard invariant at both ingestion and publication;
  source filenames alone are not sufficient location evidence.
- Keep the MVP slash-command boundary limited to channel and digest-alert-role
  administration. Do not
  connect legacy text commands, scraping, watchlists, branding, edits, or closure
  handling to `bot.py`; those features are deferred in `TODO.md`.
- Document new environment variables and schema changes.
- Do not commit secrets, local database files, or feed downloads.

## Verification

Run before opening a pull request:

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/python -m compileall -q job_bot outsource tests bot.py
```

When changing persistence, also run the Postgres path:

```bash
make test-postgres ALEMBIC=".venv/bin/python -m alembic" PYTEST=".venv/bin/python -m pytest"
```

Tests should cover real-format fixtures, schema drift, location handling, conditional
`304` fetches, URL variants, cross-source duplication, partial and complete baselines,
delivery retries, process restarts, and migration compatibility as applicable.
For category changes, also cover ordered startup setup without listings, concurrent
setup and posting, interrupted creation, recovery of permanent cards and archived threads after
restarts, disabled destinations, thread permissions, and unchanged pings.

## Pull requests and issues

Keep changes scoped and explain user-visible behavior, migrations, and new settings.
For source or dedupe issues, include the source URL, representative row, expected
canonical behavior, actual behavior, and relevant structured logs.
