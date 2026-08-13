# CS Job Poster

A focused Discord publisher for curated U.S. internships and new-grad roles.

The bot polls six feeds from five GitHub repositories, combines every successful
snapshot, removes duplicates across sources, and publishes newly added roles to one
Discord channel. It does not scrape company sites or expose bot commands in the MVP.

## Runtime behavior

- Polls every five minutes by default.
- Records the first complete inventory silently and posts only later additions.
- Fails closed when a feed is malformed, suspiciously empty, or unavailable.
- Uses conditional GitHub requests (`ETag` and `Last-Modified`) to avoid reprocessing
  unchanged feeds.
- Requires explicit U.S. evidence in Simplify's mixed-location data. Files explicitly
  scoped to U.S. roles are trusted unless a row clearly names a foreign location.
- Applies no title, category, seniority, season, or job-age filter.
- Sends internships and new-grad roles to the same `DISCORD_CHANNEL_ID`.
- Never treats an upstream edit to an existing source row as a new job.

The executable entrypoint is `bot.py`. It starts a minimal `discord.Client` without
Message Content Intent; the legacy command modules are retained only for data
compatibility and are not connected to the runtime.

## Feeds

| Feed | Ref and file | Format |
|---|---|---|
| Simplify internships | `SimplifyJobs/Summer2027-Internships` — `dev/.github/scripts/listings.json` | JSON |
| Simplify new grad | `SimplifyJobs/New-Grad-Positions` — `dev/.github/scripts/listings.json` | JSON |
| SpeedyApply internships | `speedyapply/2027-SWE-College-Jobs` — `main/README.md` | Markdown/HTML table |
| SpeedyApply new grad | `speedyapply/2027-SWE-College-Jobs` — `main/NEW_GRAD_USA.md` | Markdown/HTML table |
| sndsh internships | `sndsh404/summer-2027-internships` — `main/README.md` | Markdown table |
| Internship Engine | `zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships` — `main/docs/api/jobs.json` | JSON |

The fixed feed definitions live in `job_bot/github_feeds.py`. `config/sources.yaml`
is deliberately not a runtime switchboard for the MVP.

## Duplicate guarantees

Duplicate prevention is persistent and happens before Discord delivery:

1. Every upstream row is recorded by `(source_id, external_id)`.
2. Canonical keys cover sanitized application URLs, stable ATS requisition IDs, and a
   normalized company/title/cycle/location signature.
3. Cross-source copies with the same requisition are merged. Matching
   company/title/cycle roles merge when their locations overlap; distinct
   requisitions with disjoint locations remain distinct.
4. A transactional outbox permits only one delivery per canonical job and channel.
5. Every embed contains a stable delivery marker. Before a send or retry, the bot
   searches channel history for that marker and repairs the receipt when Discord
   accepted an earlier request whose database commit was interrupted.

Application links are normalized to HTTPS, stripped of fragments and tracking
parameters, and rejected when they contain credentials, unsafe network destinations,
known shorteners, executable downloads, or malformed URLs.

## Discord setup

Create a bot and grant it these channel permissions:

- View Channel
- Send Messages
- Embed Links
- Read Message History

Read Message History is required for duplicate reconciliation. The bot fails closed
before publishing if any permission is missing. Message Content Intent is not needed.

## Configuration

Copy `.env.example` to `.env`. These values are required for production:

```dotenv
DISCORD_TOKEN=replace-me
DISCORD_CHANNEL_ID=123456789012345678
DATABASE_URL=postgresql://jobbot:jobbot@localhost:5432/jobbot
```

Optional runtime settings:

```dotenv
POLL_INTERVAL_SECONDS=300
REQUEST_TIMEOUT_SECONDS=20
REQUEST_RETRIES=2
SOURCE_CONCURRENCY=6
PUBLISH_DELAY_SECONDS=1.0
LOG_LEVEL=INFO
USER_AGENT=cs-job-poster/2.0
```

`POLL_INTERVAL_SECONDS` has a minimum of 30 seconds. SQLite remains supported for
isolated tests and migration tooling, but the production publisher requires Postgres
so a database advisory lock can prevent competing workers.

## Local development

Python 3.11 is required.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
make postgres-up
make db-migrate
make run-postgres
```

On the first successful run, all six current inventories are saved with suppressed
deliveries and nothing is posted. A partial first run cannot complete that baseline.
Each feed added later also gets its own silent first snapshot.

Run the test suite with:

```bash
pytest tests -q
```

GitHub Actions runs the complete suite against Postgres before deployment. There is
no separate Actions publisher: only the long-running service sends messages.

## Operations

Each poll writes structured, count-oriented logs for feed fetch status, accepted and
rejected observations, new canonicals, merges, suppressed/queued deliveries, sent or
reconciled messages, and failures. Upstream errors do not erase prior snapshots or
create closure events.

Existing production tables are preserved. Alembic migration
`20260813_0004_feed_outbox` adds the authoritative observation, key, feed-state, and
delivery tables and seeds existing `discord_posts` rows as sent.

See `deploy/single-vps.md` for the supported single-worker deployment and `TODO.md`
for intentionally deferred commands, process tracking, direct scraping, closures,
post editing, branding, and enrichment.

## Important modules

- `job_bot/github_feeds.py` — feed definitions, conditional fetching, schema adapters.
- `job_bot/utils/feed_normalization.py` — U.S. classification, link safety, ATS keys.
- `job_bot/feed_pipeline.py` — concurrent collection, silent baselines, ingestion.
- `job_bot/database.py` — observations, canonical merging, delivery outbox.
- `job_bot/feed_publisher.py` — Discord embeds and marker reconciliation.
- `job_bot/mvp_runtime.py` — one-client scheduler and process lock.
