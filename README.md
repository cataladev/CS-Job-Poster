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
- Uses SQLite so jobs are not reposted after restart
- Uses a watchlist system so companies can be requested and approved over time
- Uses embeds for job posts and command output
- Uses reaction-based pagination for list-style commands such as `!help`, `!sources`, `!recent`, `!companies`, and `!companyrequests`
- Tries to show a company icon in job embeds and falls back to a troll thumbnail plus `🧌` in the company field when no safe company icon can be derived
- Includes an opt-in process tracker with visibility-scoped statuses, peer discovery, and per-company note history

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
  SQLite schema and all persistence operations.
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
- `SQLITE_DB_PATH`
- `SCHEDULED_RUN_HOURS`
- `SCHEDULE_TIMEZONE`
- `US_ONLY`
- `MAX_JOB_AGE_HOURS`
- `SOURCE_CONFIG_PATH`
- `COMPANY_SEED_PATH`

Current default behavior in [`.env.example`](.env.example):

- `SCHEDULED_RUN_HOURS=12,20`
- `SCHEDULE_TIMEZONE=America/New_York`
- `US_ONLY=true`
- `MAX_JOB_AGE_HOURS=0`
- `ENABLE_NEW_GRAD_SOURCE=false`

### 3. Discord bot setup

In the Discord developer portal:

1. Create the application and bot.
2. Enable `Message Content Intent`.
3. Invite the bot to your server with permission to read and send messages.
4. Copy the channel IDs for the internship and new-grad channels.

## Running Locally

```bash
source .venv/bin/activate
python bot.py
```

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
- `!jobstatus <company> <status>`
- `!myjobs`
- `!jobpeers <company>`
- `!jobprocess <company|id>`

List-style commands use embed pagination with reactions:

- `⬅️` previous page
- `➡️` next page
- `⏹️` clear reactions / stop browsing

Watchlist commands:

- `!requestcompany <company> | <careers_url> | [homepage_url]`
- `!companies [query]`
- `!companyinfo <company|id>`

Process tracker commands:

- `!process` join the process tracker and show usage
- `!process <company> <save|hide|applied|oa|interview|rejected|offer>` set your current status
- `!process <company|id>` view visible company process stats
- `!process <company|id> notes` view visible note history
- `!process <company|id> | <note>` append a new process note without overwriting older notes
- `!process peers <company>` find visible peers for that company

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

- The bot persists state in SQLite, so test runs affect future dedupe and posting behavior.
- If you want a clean local run, point `SQLITE_DB_PATH` at a different file instead of reusing the live DB.
- Some secondary sources are noisy or brittle. A source returning errors does not crash the whole run.
- `wellfound` and some `workday` boards can fail frequently; that is expected unless those adapters are improved.
- If `!runjobs` appears to do nothing, check whether the run found `0` new canonical jobs after filtering and dedupe.
- Company logos now use strict verified branding only: cached provider logos first, then verified branded favicons, then the troll fallback.
- Shared ATS hosts are intentionally not used as branded homepage domains. If a company is requested manually, pass a real company homepage as the optional third argument when you have one.
- `!synccompanies` performs a branding backfill and refreshes already-posted embeds when a better verified logo becomes available.

## Deployment

For a VPS, run it under `systemd`, `tmux`, or another long-lived supervisor.

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

## Working Rules For This Repo

If you are making changes:

1. Read the relevant source module first.
2. Keep source failures isolated.
3. Preserve global dedupe guarantees.
4. Run tests after changes.
5. Keep docs and `.env.example` aligned with actual runtime behavior.

If the README and the code disagree, trust the code and update the README.
