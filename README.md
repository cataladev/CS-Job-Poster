# CS Job Poster

Discord bot that discovers fresh U.S. software internship and new-grad roles, deduplicates them across sources, and posts only new jobs into Discord.

The bot is GitHub-first:

1. Pull from the SimplifyJobs GitHub repos.
2. Normalize and deduplicate those jobs.
3. Expand into company watchlist and ATS sources.
4. Deduplicate everything globally.
5. Post only brand-new canonical jobs.

This README is written for someone actively working on the codebase.

Contribution docs live in [CONTRIBUTING.md](/Users/carlos/Development/Job%20Bot/CONTRIBUTING.md).

## Current Behavior

- Posts internships to `DISCORD_CHANNEL_ID`
- Posts new-grad roles to `DISCORD_NEW_GRAD_CHANNEL_ID` when that channel is configured and `ENABLE_NEW_GRAD_SOURCE=true`
- Runs on a fixed schedule at `12:00 PM` and `8:00 PM` in `America/New_York`
- Defaults to `US_ONLY=true`
- Defaults to `MAX_JOB_AGE_HOURS=0`, which means only same-day roles are eligible to post
- Uses Postgres in production when `DATABASE_URL` is set, with SQLite fallback for local tests and throwaway runs
- Uses a watchlist system so companies can be requested and approved over time
- Uses embeds for job posts and command output
- Uses reaction-based pagination for list-style commands such as `!help`, `!sources`, `!recent`, `!companies`, and `!companyrequests`
- Tries to show a company icon in job embeds and falls back to a troll thumbnail plus `🧌` in the company field when no safe company icon can be derived
- Includes an opt-in process tracker with role-scoped statuses, peer discovery, and per-note moderation history split by track and season

## Source Order

Priority sources run first:

- `github_simplify_internships`
- `github_simplify_new_grad`

Secondary sources then run behind concurrency limits:

- `company_watchlist_internship`
- `company_watchlist_new_grad`
- `greenhouse`
- `greenhouse_new_grad`
- `lever`
- `lever_new_grad`
- `ashby`
- `ashby_new_grad`
- `workday`
- `workday_new_grad`
- `yc`
- `wellfound`

Source config lives in [`config/sources.yaml`](config/sources.yaml).

## Project Layout

High-signal files:

- [`bot.py`](bot.py)
  Thin entrypoint.
- [`job_bot/bot.py`](job_bot/bot.py)
  Discord client, commands, startup wiring.
- [`job_bot/pipeline.py`](job_bot/pipeline.py)
  Main run pipeline: fetch, normalize, filter, dedupe, persist, publish.
- [`job_bot/source_manager.py`](job_bot/source_manager.py)
  Priority vs secondary source execution and failure isolation.
- [`job_bot/publisher.py`](job_bot/publisher.py)
  Discord embeds, logo handling, repeat guard.
- [`job_bot/filters.py`](job_bot/filters.py)
  Role filtering, U.S.-only logic, age gating.
- [`job_bot/dedupe.py`](job_bot/dedupe.py)
  Exact, strong, and fuzzy dedupe.
- [`job_bot/database.py`](job_bot/database.py)
  Postgres-first persistence layer, schema bootstrap, and SQLite fallback for tests/local runs.
- [`job_bot/process_tracking.py`](job_bot/process_tracking.py)
  Shared process-tracking rules for role scopes, season/track parsing, moderation, and next-action guidance.
- [`job_bot/watchlist.py`](job_bot/watchlist.py)
  Watchlist import, company requests, target resolution.
- [`job_bot/sources/`](job_bot/sources)
  Individual source adapters.
- [`tests/`](tests)
  Coverage for parser, filters, dedupe, scheduler, publisher, pipeline, watchlist.

## Setup

### 1. Create the Python environment

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Create your env file

```bash
cp .env.example .env
```

Important env values:

- `DISCORD_TOKEN`
- `DISCORD_CHANNEL_ID`
- `DISCORD_NEW_GRAD_CHANNEL_ID`
- `ENABLE_NEW_GRAD_SOURCE`
- `DATABASE_URL`
- `SQLITE_DB_PATH`
- `SCHEDULED_RUN_HOURS`
- `SCHEDULE_TIMEZONE`
- `US_ONLY`
- `MAX_JOB_AGE_HOURS`
- `SOURCE_CONFIG_PATH`
- `COMPANY_SEED_PATH`

Current default behavior in [`.env.example`](.env.example):

- `POSTGRES_HOST_PORT=5432`
- `DATABASE_URL=postgresql://jobbot:jobbot@localhost:5432/jobbot`
- `SCHEDULED_RUN_HOURS=12,20`
- `SCHEDULE_TIMEZONE=America/New_York`
- `US_ONLY=true`
- `MAX_JOB_AGE_HOURS=0`
- `ENABLE_NEW_GRAD_SOURCE=false`

Database behavior:

- `DATABASE_URL` is the primary production setting and should point at Postgres
- `SQLITE_DB_PATH` is only used when `DATABASE_URL` is empty
- tests still use SQLite by default because they create isolated temp databases quickly

### 3. Discord bot setup

In the Discord developer portal:

1. Create the application and bot.
2. Enable `Message Content Intent`.
3. Invite the bot to your server with permission to read and send messages.
4. Copy the channel IDs for the internship and new-grad channels.

## Development

Primary local workflow is Postgres-backed. Docker Compose is the default database path for development.

### Local dev quickstart

Set these values in `.env` for local development:

- `POSTGRES_HOST_PORT=5432`
- `DATABASE_URL=postgresql://jobbot:jobbot@localhost:5432/jobbot`
- `DISCORD_TOKEN`
- `DISCORD_CHANNEL_ID`
- optionally `DISCORD_NEW_GRAD_CHANNEL_ID`

Primary local commands:

```bash
make postgres-up
make db-migrate
make db-import-sqlite      # optional; imports existing job_bot.db into Postgres
make dev-postgres
```

Full local startup sequence:

```bash
cd "/Users/carlos/Development/Job Bot"
source .venv/bin/activate
make postgres-up
make db-migrate
make dev-postgres
```

If port `5432` is already in use on your machine, pick a different host port in `.env` and keep `DATABASE_URL` in sync. Example:

```env
POSTGRES_HOST_PORT=5433
DATABASE_URL=postgresql://jobbot:jobbot@localhost:5433/jobbot
```

What each command does:

- `make postgres-up` starts the local Postgres container and waits until it is ready
- `make db-migrate` applies Alembic migrations to the configured `DATABASE_URL`
- `make db-import-sqlite` copies data from `SQLITE_DB_PATH` into Postgres once
- `make dev-postgres` starts Postgres, runs migrations, optionally imports SQLite data when `IMPORT_SQLITE=1`, then runs `python bot.py`

Example first run if you want to preserve the current local SQLite data:

```bash
make postgres-up
make db-migrate
make db-import-sqlite
make dev-postgres
```

Example first run if you want a fresh Postgres database:

```bash
make postgres-up
make db-migrate
make dev-postgres
```

If `make postgres-up` fails with `password authentication failed for user "jobbot"`, your local Docker volume was initialized with different credentials. Reset the local Postgres volume once and retry:

```bash
docker compose down -v
make postgres-up
make db-migrate
make dev-postgres
```

You should not need to install `psycopg2-binary` separately. Alembic is configured to use the existing `psycopg` dependency from `requirements.txt`.

The direct entrypoint is still:

```bash
source .venv/bin/activate
python bot.py
```

But that is the underlying app command, not the preferred development workflow.

SQLite is still acceptable for isolated tests or scratch local runs, but not as the normal development path for the real app behavior.

On startup, expect logs showing:

- watchlist seed sync
- Discord login
- bot ready
- scheduler started at `12, 20:00 (America/New_York)`

## Testing

Full suite:

```bash
source .venv/bin/activate
pytest tests -q
```

If your shell environment has temp-dir issues, this is a safe fallback:

```bash
mkdir -p .tmp
TMPDIR=$PWD/.tmp source .venv/bin/activate
TMPDIR=$PWD/.tmp pytest tests -q
```

Postgres integration test path:

```bash
make test-postgres
```

That command:

- starts the local Postgres service if needed
- applies Alembic migrations
- runs the Postgres integration smoke test in [`tests/test_postgres_backend.py`](/Users/carlos/Development/Job%20Bot/tests/test_postgres_backend.py)

Syntax check:

```bash
PYTHONPYCACHEPREFIX=.pycache python3 -m py_compile $(find job_bot tests -name '*.py' -print) bot.py
```

## Contributing And Issues

- Read [CONTRIBUTING.md](/Users/carlos/Development/Job%20Bot/CONTRIBUTING.md) before making structural changes.
- Use GitHub Issues for bugs, source regressions, and feature requests.
- Issue templates live under [`.github/ISSUE_TEMPLATE/`](/Users/carlos/Development/Job%20Bot/.github/ISSUE_TEMPLATE) and are meant to capture the minimum context needed to debug source, dedupe, and Discord behavior.
- Keep changes small, tested, and documented when commands, schema, or env behavior changes.

## Manual Discord Commands

User commands:

- `!ping`
- `!help`
- `!helpjobs`
- `!runjobs`
- `!stats`
- `!sources`
- `!recent`
- `!dedupecheck <url or text>`
- `!season`
- `!summary [daily|weekly]`
- `!process`
- `!visibility [open|matched|private]`
- `!jobstatus <scope_id> <status>`
- `!myjobs`
- `!jobpeers <scope_id>`
- `!jobprocess <scope_id>`

List-style commands use embed pagination with reactions:

- `⬅️` previous page
- `➡️` next page
- `⏹️` clear reactions / stop browsing

Watchlist commands:

- `!requestcompany <company> | <careers_url> | [homepage_url]`
- `!companies [query]`
- `!companyinfo <company|id>`

Process tracker commands:

- `!process` opt in and show your active tracked roles in DM
- `!process set <company> | <role> | <season> | <status>` create or update a role scope
- `!process set <company> | <role> | <track> | <season> | <status>` when track cannot be inferred cleanly
- `!process company <company>` list tracked role scopes for that company
- `!process view <scope_id>` show visible process state for one role scope
- `!process status <scope_id> <status>` update one role scope quickly
- `!process peers <scope_id>` find visible peers for one role scope
- `!process notes <scope_id>` read visible note history for one role scope
- `!process note <scope_id> | <note>` append a new note without overwriting older notes
- `!process report <note_id> [reason]` report leaked/private content
- `!process deletenote <note_id> [reason]` delete your own note, or admin-delete a bad note
- `!process open|matched|private` change discovery visibility for peers; command replies still stay in DM by default

Admin-only commands:

- `!runjobs`
- `!stats`
- `!sources`
- `!dedupecheck <url or text>`
- `!season [value]`
- `!summary [daily|weekly]`
- `!companyrequests [pending|approved|denied|all]`
- `!approvecompany <request_id>`
- `!denycompany <request_id> [reason]`
- `!synccompanies`

Admin-only commands require Discord `Administrator` permission in the server.

## How the Pipeline Works

The main run path is in [`job_bot/pipeline.py`](job_bot/pipeline.py).

Per run:

1. Open one shared async HTTP client.
2. Run priority sources first.
3. Normalize every raw job.
4. Discover ATS seeds from URLs.
5. Apply filters.
6. Resolve duplicates.
7. Save canonical jobs and variants.
8. Publish any newly eligible canonicals.
9. Run secondary sources and publish incrementally as they finish.

Important implementation detail:

- the bot publishes incrementally during a run, not only at the very end
- list-style Discord commands are rendered as paginated embeds instead of raw text dumps

## Filtering Rules

Filtering lives in [`job_bot/filters.py`](job_bot/filters.py).

The bot is intentionally strict:

- software-focused roles only
- internship or new-grad signals required
- rejects senior/staff/manager roles
- rejects unrelated engineering disciplines
- rejects non-U.S. roles when `US_ONLY=true`
- rejects unknown-age jobs
- rejects anything older than the configured age window

Current practical default:

- U.S.-only
- same-day jobs only
- unknown-age jobs are rejected

## Deduplication

Deduplication lives in [`job_bot/dedupe.py`](job_bot/dedupe.py).

It uses layered matching:

1. Exact canonical URL
2. External ATS/job IDs
3. Strong fingerprint on normalized company, title, season, and location
4. Fuzzy fallback with company/title/location similarity

The database stores:

- canonical jobs
- job variants
- fingerprints
- Discord post records
- dedupe match reasons

This is what prevents reposting across restarts and across platforms.

Production recommendation:

- run the bot against Postgres through `DATABASE_URL`
- keep SQLite for tests, ad hoc local runs, or one-off fixtures

## GitHub Simplify Ingestion

GitHub parsing lives in [`job_bot/sources/github_simplify.py`](job_bot/sources/github_simplify.py).

It currently pulls from:

- `SimplifyJobs/Summer2026-Internships`
- `SimplifyJobs/New-Grad-Positions`

It handles:

- markdown tables
- continuation rows
- multi-location cells
- inactive sections
- row hashing for change tracking

This source is the main broad-discovery feed.

## Watchlist System

Watchlist logic lives in [`job_bot/watchlist.py`](job_bot/watchlist.py).

The watchlist is used to drive company-specific discovery after the GitHub pass.

It includes:

- seeded companies from [`data/company_watchlist_seed.csv`](data/company_watchlist_seed.csv)
- user-submitted company requests
- approval flow for requests
- resolved scan targets like `greenhouse`, `lever`, `ashby`, `workday`, and `company_page`

If you want the bot to cover a company reliably, this is the first place to look.

## Process Tracking

Process tracking is role-scoped, not company-scoped.

That means:

- `Software Engineer Intern • Summer 2027` and `Software Engineer • New Grad • Fall 2027` are different scopes
- notes are stored in their own table and preserved as history instead of being overwritten into one field
- peer discovery is filtered by your visibility plus the exact role scope
- bot replies for process commands are DM-first to avoid leaking sensitive recruiting details into public channels

Visibility affects who can discover your role scope:

- `private`: only you can see your process data
- `matched`: other users only see you when they track the same role scope too
- `open`: anyone in the process community can discover your visible role scope

Moderation and anti-spam guardrails:

- notes block links, contact info, and common leaked/private content patterns
- note creation, status updates, and note reports are rate-limited
- users can report notes
- users can delete their own notes
- server admins can delete problematic notes

What `!process` should show in practice:

- the active companies and exact role scopes you are tracking
- the last update time for each scope
- a “Next:” prompt that tells you whether to follow up, wait, finish an OA, or update where the process is stuck

## Most Common Changes

### Add or tune a source

1. Edit or add a source module under [`job_bot/sources/`](job_bot/sources).
2. Register it in [`job_bot/sources/__init__.py`](job_bot/sources/__init__.py).
3. Add config in [`config/sources.yaml`](config/sources.yaml).
4. Add or update tests.

### Change posting rules

Look here:

- [`job_bot/filters.py`](job_bot/filters.py)
- [`job_bot/publisher.py`](job_bot/publisher.py)
- [`.env.example`](.env.example)

### Change dedupe behavior

Look here:

- [`job_bot/dedupe.py`](job_bot/dedupe.py)
- [`job_bot/utils/normalization.py`](job_bot/utils/normalization.py)
- [`job_bot/database.py`](job_bot/database.py)

### Change the schedule

Edit:

- `SCHEDULED_RUN_HOURS`
- `SCHEDULE_TIMEZONE`

Scheduler code is in [`job_bot/scheduler.py`](job_bot/scheduler.py).

## Operational Notes

- In production, the bot should persist state in Postgres through `DATABASE_URL`.
- Local tests and local scratch runs still default to SQLite, so those test runs affect future dedupe and posting behavior if they reuse the same local file.
- If you want a clean local run, point `SQLITE_DB_PATH` at a different file instead of reusing the same local SQLite file.
- Some secondary sources are noisy or brittle. A source returning errors does not crash the whole run.
- `wellfound` and some `workday` boards can fail frequently; that is expected unless those adapters are improved.
- If `!runjobs` appears to do nothing, check whether the run found `0` new canonical jobs after filtering and dedupe.
- Company logos now use strict verified branding only: cached provider logos first, then verified branded favicons, then the troll fallback.
- Shared ATS hosts are intentionally not used as branded homepage domains. If a company is requested manually, pass a real company homepage as the optional third argument when you have one.
- `!synccompanies` performs a branding backfill and refreshes already-posted embeds when a better verified logo becomes available.
- Process commands default to DM replies in guilds. Visibility changes peer discovery only; it does not make command output public.
- Legacy company-only process data is still migrated into synthetic `General / Legacy` scopes so older local databases do not lose history.

## Production

Production must use Postgres through `DATABASE_URL`.

Minimum production requirements:

- Python 3.11
- Postgres
- `.env` with `DATABASE_URL`
- Discord bot token and channel IDs

Recommended deployment flow:

1. Clone the repo on the server.
2. Create the virtualenv.
3. Install requirements.
4. Create `.env`.
5. Run Alembic migrations.
6. Optionally import legacy SQLite data once.
7. Start the bot under `systemd`.

Example:

```bash
git clone https://github.com/cataladev/CS-Job-Poster.git
cd CS-Job-Poster
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
.venv/bin/alembic upgrade head
.venv/bin/python scripts/import_sqlite_to_postgres.py   # optional, one-time
```

After that, run the bot under a long-lived supervisor such as `systemd`.

Example `systemd` unit:

```ini
[Unit]
Description=CS Job Poster
After=network.target

[Service]
WorkingDirectory=/opt/cs-job-poster
ExecStart=/opt/cs-job-poster/.venv/bin/python bot.py
Restart=always
RestartSec=5
EnvironmentFile=/opt/cs-job-poster/.env

[Install]
WantedBy=multi-user.target
```

Before starting or restarting the service on a deploy, run migrations against the production `DATABASE_URL`:

```bash
cd /opt/cs-job-poster
source .venv/bin/activate
alembic upgrade head
systemctl restart cs-job-poster
```

Update / redeploy sequence:

```bash
cd /opt/cs-job-poster
git pull --ff-only
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
systemctl restart cs-job-poster
```

## Working Rules For This Repo

If you are making changes:

1. Read the relevant source module first.
2. Keep source failures isolated.
3. Preserve global dedupe guarantees.
4. Run tests after changes.
5. Keep docs and `.env.example` aligned with actual runtime behavior.

If the README and the code disagree, trust the code and update the README.
