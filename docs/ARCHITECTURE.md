# Architecture

## Overview

CS-Job-Poster is a Discord bot that aggregates U.S. computer science internship and new-grad job postings from 12+ sources, deduplicates them, and publishes to Discord channels. It runs as a single long-lived process on a VPS with a Postgres database.

## Feed Collection

Two independent poll loops run on separate timers:

### Core Feed Loop (`FeedPipeline`)

Polls every `poll_interval_seconds` (default 300s, Jobright every 60s). Collects from:

- **GitHub markdown feeds** -- parses README tables from repos like SimplifyJobs, SpeedyApply, sndsh404, ApplyGuy
- **Jobright** -- queries the visitor API taxonomy window for intern/new-grad CS roles, falls back to listing page scrape on failure. Supports ISP proxy rotation for rate limiting.
- **Direct ATS lookup** -- employer boards from `data/company_watchlist_seed.csv` plus curated boards enrich current Jobright jobs with application links, with a four-hour cache. They are not independent discovery feeds.
- **Internship Engine** -- JSON feed + README fallback

Each feed has a `FeedDefinition` with source ID, parser, provider, and poll interval. `GitHubFeedFetcher` handles provider-specific API calls (conditional requests via ETag/Last-Modified for GitHub, POST APIs for Workday/ByteDance/Rippling/Jobright).

### Company Source Loop (`CompanySourcePipeline`)

Polls every `company_source_poll_interval_seconds`. Runs registered first-party company API adapters with bounded concurrency. Currently: IBM, Microsoft.

Each adapter implements the `CompanySource` protocol: `__init__(http_client)` and `async fetch() -> CompanySourceResult`. The runner handles feed state, baseline tracking, and error recording.

Company sources only deliver jobs with a same-day `posted_date` to prevent flooding Discord with historical listings.

## Processing Pipeline

Both loops feed into shared processing:

1. **Observation** -- every source produces `JobObservation` objects with normalized fields
2. **Canonical merging** -- `dedupe.py` groups observations by URL (after ATS-key normalization) and title/company similarity into canonical jobs
3. **Delivery outbox** -- new canonical jobs get a `pending` delivery row per target channel
4. **Publishing** -- `TrackChannelPublisher.publish_pending()` sends embeds to Discord, moves deliveries through `sending` -> `sent`

### Deduplication

Multi-layer:
- URL normalization strips tracking params, resolves ATS redirects
- ATS key extraction (Greenhouse board+ID, Lever posting ID, etc.)
- Title + company fuzzy matching for cross-source duplicates
- Feed baseline epoch system prevents re-delivery after parser upgrades

## Publishing

`TrackChannelPublisher` handles all Discord output:

- **Job embeds** -- company logo, title, location, apply link, source attribution
- **Track routing** -- `internship` and `new_grad` channels configured via `/set-job-channel`
- **Category mirrors** -- optional `outsource/mirror.py` creates 9 permanent threads (SWE, DATA/AI, PM, etc.) and cross-posts jobs to matching categories
- **Daily digest** -- periodic summary embed with job counts and role alert mentions
- **Subscription alerts** -- `/target company` and `/target-location` let members get pinged for specific employers or cities
- **Delivery reconciliation** -- recovers from bot crashes by checking channel history for message IDs

## Operational Logs

- `LogThreads` creates or recovers two permanent bot-authored starter cards and threads in `DISCORD_LOG_CHANNEL_ID` at startup.
- `ScrapeStatusReporter` routes core summaries to **Core feeds** and company-source summaries to **Direct scrapers**.
- `CoreLogNotifier` sends Jobright health transitions and session alerts through the same core thread, using the bot and an environment label; standalone tools retain optional webhook notifications.
- A shared setup lock prevents overlapping startup, poll, and notification calls from creating extra threads. Marker recovery has no date cutoff and reuses archived threads and interrupted setup across restarts.
- Company-source error mentions and configured Jobright operator mentions are preserved. Locked threads or permission failures are logged locally, without falling back to parent-channel messages.

## Commands

All commands are guild-scoped (instant sync) and admin-only unless noted:

| Command | Purpose |
|---------|---------|
| `/health [view]` | Bot health, feed status, delivery queue (admin) |
| `/set-job-channel` | Configure internship/new-grad posting channels (admin) |
| `/create-job-channels` | One-step channel + category setup (admin) |
| `/set-job-alert-role` | Set the role pinged in daily digests (admin) |
| `/job-alert` | Member opt-in to daily digest role (everyone) |
| `/target company` | Subscribe to a company's postings (everyone) |
| `/target-location` | Subscribe to a location's postings (everyone) |

## Database

Postgres in production, SQLite for tests. Key tables:

- `canonical_jobs` -- deduplicated job records
- `job_observations` -- raw source observations linked to canonicals
- `delivery_outbox` -- per-channel delivery state machine (pending -> sending -> sent/suppressed/uncertain)
- `feed_state` -- per-source last-checked, last-success, errors, baseline status
- `app_state` -- key-value store (baseline epoch, feature flags)
- `runtime_config` -- channel IDs and role IDs set via slash commands
- `command_receipts` -- audit log of admin command usage
- `company_targets` / `location_targets` -- member subscription tables

Schema migrations managed by Alembic.

## Configuration

All configuration via environment variables (see `.env.example`):

- `DISCORD_TOKEN`, `DISCORD_CHANNEL_ID` -- required
- `DATABASE_URL` -- Postgres connection string (required in production)
- `BOT_ENV` -- environment label shown in logs and health embeds
- `POLL_INTERVAL_SECONDS`, `COMPANY_SOURCE_POLL_INTERVAL_SECONDS` -- collection frequency
- `JOBRIGHT_SESSION`, `JOBRIGHT_PROXY_*` -- Jobright-specific auth and proxy config
- `DISCORD_LOG_CHANNEL_ID` -- parent channel for the permanent Core feeds and Direct scrapers log threads
- `JOBRIGHT_STATUS_WEBHOOK_URL` -- optional operational-alert webhook for standalone scraper tools

Runtime config (channel IDs, roles) stored in database and overridable via slash commands.
