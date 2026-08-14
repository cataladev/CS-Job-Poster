# Contributing

This project is a two-channel Discord publisher for curated U.S. internships and
new-grad roles. Read `README.md` before changing runtime behavior.

## Setup

Use Python 3.11 and Postgres for application runs:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
make postgres-up
make db-migrate
make run-postgres
```

SQLite is supported for isolated unit tests only.

## Working agreement

- Keep source-specific parsing in `job_bot/github_feeds.py` and use typed
  `JobObservation` values at the shared boundary.
- Treat duplicate prevention as a correctness invariant. New keys may merge existing
  canonicals, so every change needs positive and negative merge cases.
- Preserve the silent-baseline rule: incomplete initial snapshots must never publish.
- Treat malformed or suspiciously empty feeds as unavailable, not as evidence that
  jobs disappeared.
- Keep Discord sending behind the transactional outbox and marker reconciliation.
- Never weaken safe-link validation to rescue a single malformed upstream row.
- Treat U.S.-only delivery as a hard invariant at both ingestion and publication;
  source filenames alone are not sufficient location evidence.
- Keep the MVP slash-command boundary limited to channel administration. Do not
  connect legacy text commands, scraping, watchlists, branding, edits, or closure
  handling to `bot.py`; those features are deferred in `TODO.md`.
- Document new environment variables and schema changes.
- Do not commit secrets, local database files, or feed downloads.

## Verification

Run before opening a pull request:

```bash
pytest tests -q
PYTHONPYCACHEPREFIX=.pycache python3 -m py_compile $(find job_bot tests -name '*.py' -print) bot.py
```

When changing persistence, also run the Postgres path:

```bash
make test-postgres
```

Tests should cover real-format fixtures, schema drift, location handling, conditional
`304` fetches, URL variants, cross-source duplication, partial and complete baselines,
delivery retries, process restarts, and migration compatibility as applicable.

## Pull requests and issues

Keep changes scoped and explain user-visible behavior, migrations, and new settings.
For source or dedupe issues, include the source URL, representative row, expected
canonical behavior, actual behavior, and relevant structured logs.
