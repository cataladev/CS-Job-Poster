# GitHub-First Internship Aggregator Discord Bot

Production-oriented Python Discord bot that ingests high-signal internship feeds from GitHub first, then expands into seeded ATS/job-board discovery, aggressively deduplicates everything into canonical opportunities, and posts only new jobs to Discord.

## Features

- GitHub-first ingestion from:
  - `SimplifyJobs/Summer2026-Internships`
  - `SimplifyJobs/New-Grad-Positions`
- Async source architecture using `aiohttp`
- Fixed run pipeline:
  1. GitHub Simplify repos
  2. Normalize and dedupe
  3. Auto-discover ATS seeds from apply URLs
  4. Query downstream sources
  5. Global dedupe
  6. Post only new canonical jobs
- Strong layered deduplication:
  - canonical URL and external job IDs
  - strong fingerprint on normalized company/title/season/location
  - fuzzy fallback via RapidFuzz
- SQLite persistence for canonical jobs, variants, fingerprints, Discord posts, source history, dedupe matches, GitHub state, and discovered board seeds
- Discord commands for health, manual runs, stats, recent jobs, sources, dedupe checks, and summaries

## Project Layout

```text
bot.py
config/sources.yaml
job_bot/
  bot.py
  config.py
  database.py
  dedupe.py
  filters.py
  models.py
  pipeline.py
  publisher.py
  scheduler.py
  source_manager.py
  sources/
  utils/
tests/
```

## Setup

1. Create a Discord bot application in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Enable the `MESSAGE CONTENT INTENT` under Bot settings if you want the prefix commands such as `!ping` and `!runjobs` to work. Without that toggle, the scheduled bot can still connect only if the code is changed to avoid privileged intents, but the current command-based setup requires it.
3. Invite the bot to your server with permissions to read and send messages in the target channel.
4. Copy your bot token and channel IDs into `.env`.

### Getting the Discord Channel ID

1. Enable Developer Mode in Discord.
2. Right-click the internship channel and choose `Copy Channel ID`.
3. If you want a separate new-grad feed, repeat for that channel and set `DISCORD_NEW_GRAD_CHANNEL_ID`.

## Environment Variables

See `.env.example`. Important values:

- `DISCORD_TOKEN`
- `DISCORD_CHANNEL_ID`
- `DISCORD_NEW_GRAD_CHANNEL_ID`
- `CRON_INTERVAL_MINUTES`
- `SQLITE_DB_PATH`
- `REMOTE_ONLY`
- `US_ONLY`
- `INCLUDE_COOPS`
- `SEASON_FILTER`
- `COMPANY_BLACKLIST`
- `KEYWORD_ALLOWLIST`
- `KEYWORD_DENYLIST`
- `ENABLE_PLAYWRIGHT`
- `ENABLE_NEW_GRAD_SOURCE`
- `ENABLED_SOURCES`
- `SOURCE_CONFIG_PATH`
- `MAX_JOB_AGE_HOURS`

## Install and Run

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

## Source Configuration

`config/sources.yaml` controls:

- source enablement
- source priority
- track routing (`internship` vs `new_grad`)
- static board seeds
- listing URLs for HTML-based sources
- company career pages

The bot also auto-discovers `greenhouse`, `lever`, `ashby`, and `workday` seeds from GitHub apply URLs and stores them in SQLite, so downstream coverage expands automatically over time.

Note: the live SimplifyJobs repositories currently resolve from the `dev` branch in `config/sources.yaml`. If those repos change their default branch later, update the branch setting there.

## How GitHub Ingestion Works

- The GitHub source runs first every cycle.
- It fetches raw markdown with conditional requests using stored ETags.
- It parses `README.md`, `README-Off-Season.md`, and `README-Inactive.md` from the internship repo plus `README.md` from the new-grad repo.
- HTML tables are parsed robustly, including:
  - continuation rows (`↳`)
  - multi-location `<details>` cells
  - optional `Terms` columns
  - inactive sections
- The source stores a stable row hash so new or changed rows can be tracked cleanly.

## How Deduplication Works

Each listing is normalized before dedupe:

- tracking parameters are removed from URLs
- external ATS IDs are extracted when possible
- company names are normalized
- title variants like `Software Engineering Intern` and `Software Engineer Intern` collapse to the same normalized form
- shorthand locations like `NYC` and `Remote in USA` are normalized
- season and year are derived from title, terms, summary, and raw source metadata

Then the bot applies three layers:

1. Exact match on canonical URL or external job ID
2. Strong fingerprint on normalized company + title + season + location
3. Fuzzy similarity fallback with company/title/location scoring and merge thresholds

All match reasons are persisted in `dedupe_matches`.

## Adding a New Source

1. Create a new module under `job_bot/sources/`.
2. Subclass `BaseSource`.
3. Implement `fetch(context) -> SourceResult`.
4. Register the class in `job_bot/sources/__init__.py`.
5. Add a config entry in `config/sources.yaml`.

Recommended pattern:

- prefer official/public APIs where available
- fall back to stable structured HTML or JSON-LD
- return empty results on failure instead of raising uncaught exceptions

## Deployment

### Local

Run `python bot.py` inside a long-lived shell, `tmux`, or `screen`.

### VPS / systemd

Create a virtual environment, install dependencies, and run the bot behind a `systemd` service with restart enabled.

Example service:

```ini
[Unit]
Description=Internship Aggregator Discord Bot
After=network.target

[Service]
WorkingDirectory=/opt/job-bot
ExecStart=/opt/job-bot/.venv/bin/python bot.py
Restart=always
RestartSec=5
EnvironmentFile=/opt/job-bot/.env

[Install]
WantedBy=multi-user.target
```

## Testing

```bash
pytest
```

For local runtime testing:

```bash
source .venv/bin/activate
python bot.py
```

With the current defaults, the bot only publishes jobs with a known age of `24` hours or less. GitHub rows use the Simplify `Age` column when present, and ATS sources fall back to their `posted_date` metadata.

If you hit `discord.errors.PrivilegedIntentsRequired`, enable Message Content Intent for the bot in the Discord developer portal and restart it.

## Limitations and Notes

- LinkedIn, Indeed, and Handshake are included as experimental opt-in sources and are disabled by default.
- Wellfound and Y Combinator use public HTML discovery and may need selector updates over time.
- Workday coverage depends on valid discovered or configured seeds.
- Some ATS platforms use custom domains that do not expose enough board metadata to auto-discover every listing source perfectly.

## Responsible Scraping Guidance

- Respect platform terms, robots, and rate limits.
- Prefer official/public APIs and structured feeds first.
- Use low concurrency and backoff.
- Disable unstable sources if they begin failing or producing noise.
- Review any source that requires authentication, CAPTCHA bypassing, or aggressive anti-bot evasion before enabling it in production.
