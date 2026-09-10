# CS Job Poster

A focused Discord publisher for curated U.S. internships and new-grad roles.

The bot polls U.S. aggregator surfaces, direct public employer ATS boards, and
first-party company careers APIs. It combines every successful snapshot, removes
duplicates across sources, and publishes newly added roles to separate internship and
new-grad Discord channels. Its MVP commands configure posting channels and optional
digest-alert roles, plus personal company and location targets.

## Runtime behavior

- Ticks every minute by default. Each feed states its own cadence, so the fast tick
  serves Jobright without dragging the rest along: Jobright is polled every minute,
  the GitHub repository feeds every five.
- Polls registered first-party company APIs independently every hour by default; IBM is
  the first registered company source.
- Runs the GitHub and Jobright core feeds in their own loop, so a slow
  direct-employer sweep cannot delay new internship or new-grad Discord posts.
- Derives 309 unique direct employer boards from the bundled company seed: Greenhouse,
  Lever, Ashby, and SmartRecruiters. Those boards use their public structured endpoints
  and are polled every 15 minutes by default, independently of the main poll cadence.
- Direct boards are filtered to internship and new-grad signals before ingestion; a
  board with no matching roles is a healthy empty result, not a failed source.
- Posts each newly discovered role immediately to its configured track channel,
  with direct mentions only for members targeting that company and track.
- Sends one Daily Brief per track at 4 PM New York time by default when there are
  new deliveries to summarize. Each brief links to the original Discord job posts
  and can optionally ping a track-specific role. Briefs stay in the primary channels.
- Provides optional quieter internship and new-grad channels with nine supported
  role categories. Startup creates the full ordered set of permanent cards and
  attached threads, with SWE first and OTHER last, and reuses it on later starts.
- Records the first complete core-feed inventory silently; company sources may post
  only same-day listings and never backfill prior-day inventory.
- Fails closed when a feed is malformed, suspiciously empty, or unavailable.
- Uses conditional GitHub requests (`ETag` and `Last-Modified`) to avoid reprocessing
  unchanged repository feeds. Jobright's dynamic public window is requested
  unconditionally, because the listing page's cache validators can be unchanged while
  the window behind it has moved on; it keeps its own one-minute interval.
- Requires row-level U.S. evidence. Mixed-location rows retain only their U.S.
  locations; foreign, ambiguous, archived, and inactive rows are not published.
- Groups mirrored jobs by role at the final posting step without adding category
  exclusions to ingestion. Unclear roles go to OTHER.
- Sends internships to `DISCORD_CHANNEL_ID` and new-grad roles to
  `DISCORD_NEW_GRAD_CHANNEL_ID`.
- Suppresses new-grad delivery when its channel is unset or matches the internship
  channel; it never falls back to posting new-grad roles with internships.
- Never treats an upstream edit to an existing source row as a new job.

The executable entrypoint is `bot.py`. It starts a minimal `discord.Client` without
Message Content Intent and exposes focused setup and subscription slash commands. The legacy text
command modules are retained only for data compatibility and are not connected to
the runtime.

## Feeds

Individual job footers include compact source codes after the `jobbot` delivery ID.
See [SOURCE_CODES.md](SOURCE_CODES.md) for the GitHub, Jobright, and direct-scraper mappings.

| Feed | Ref and file | Format |
|---|---|---|
| Simplify summer internships | `SimplifyJobs/Summer2027-Internships` — `dev/README.md` | Markdown/HTML table |
| Simplify off-season internships | `SimplifyJobs/Summer2027-Internships` — `dev/README-Off-Season.md` | Markdown/HTML table |
| Simplify new grad | `SimplifyJobs/New-Grad-Positions` — `dev/README.md` | Markdown/HTML table |
| SpeedyApply internships | `speedyapply/2027-SWE-College-Jobs` — `main/README.md` | Markdown/HTML table |
| SpeedyApply new grad | `speedyapply/2027-SWE-College-Jobs` — `main/NEW_GRAD_USA.md` | Markdown/HTML table |
| sndsh internships | `sndsh404/summer-2027-internships` — `main/README.md` | Markdown table |
| Internship Engine | `zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships` — `main/docs/api/jobs.json` | JSON |
| Internship Engine README | `zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships` — `main/README.md` | Markdown/HTML table |
| Jobright computer-science internships | `jobright.ai` anonymous visitor API | Taxonomy-scoped JSON window (SSR listing fallback) |
| Jobright computer-science new grad | `jobright.ai` anonymous visitor API | Taxonomy-scoped JSON window (SSR listing fallback) |

In addition, the active runtime builds a reviewed direct-employer board index from the
supported ATS URLs in `data/company_watchlist_seed.csv`: currently Greenhouse, Lever,
Ashby, and SmartRecruiters boards. When a Jobright role's company has a matching board,
the runtime requests that public API only if its local result is more than four hours old,
then uses an unambiguous direct match while retaining Jobright as fallback. This avoids
polling every employer board on every cycle and does not use HTML rendering.

Jobright's own listings can additionally be resolved to the employer's application URL.
Its public window carries only `jobright.ai` wrapper links, but a role's detail route
returns the employer's posting to a signed-in session. Setting `JOBRIGHT_SESSION_ID`
turns this on: each newly seen role costs one ~12 KB JSON request, the result is
remembered for the life of the process, and the Jobright link is kept as the fallback
shown beneath "Apply directly". Leaving it unset changes nothing — every link stays a
Jobright link. The cookie expires; when it does, the bot reports it once, stops trying,
and every link falls back, so a stale session degrades the links and never the feed.

First-party company sources live under `job_bot/company_sources/companies/`. They fetch
company-owned careers APIs independently of Jobright, adapt results to the shared
`JobObservation` contract, and enter the same normalization, deduplication, PostgreSQL
outbox, and Discord delivery path. They never backfill Discord: only listings dated
today in `SCHEDULE_TIMEZONE` may be delivered, including on the first run or after an
adapter's query expands. Older and undated rows may be retained for deduplication but
remain suppressed.

The fixed feed definitions live in `job_bot/github_feeds.py`. `config/sources.yaml`
is deliberately not a runtime switchboard for the MVP. The Jobright integration names
its computer-science job functions directly in the anonymous visitor query
(`JOBRIGHT_TAXONOMY_IDS`) rather than deriving them from the server-rendered listing,
whose `00-00-00` fallback taxonomy limited the source to one broad software-engineering
slice. Both tracks share a single most-recent window, so the whole integration costs one
request per attempt, bounded to one attempt a minute. It avoids account-specific
recommendations and unbounded scraping. If the visitor API refuses the window, the
listing snapshot is used as a stopgap, the poll is recorded as degraded rather than
healthy, and the feed backs off progressively (a minute, then five, then ten, rising
in five-minute steps to an hour)
until a window succeeds. Jobright throttles by source address, so a refused deployment
stops adding traffic instead of retrying into the block.

Because that throttle is per address, Jobright requests can optionally leave through an
ISP proxy pool. `JOBRIGHT_PROXY_FILE` names a list file with one proxy per line, which
is how a provider's export is used unedited; `JOBRIGHT_PROXY_URLS` takes the same
entries inline, comma-separated, for a short pool. Both accept `host:port:user:pass`,
`user:pass:host:port`, `user:pass@host:port`, bare `host:port`, and full `http://` URLs,
with `#` comments and blank lines ignored and duplicates collapsed; entries carrying no
credentials of their own fall back to `JOBRIGHT_PROXY_USERNAME`/`JOBRIGHT_PROXY_PASSWORD`.
Keep the list beside the env file (`/etc/job-bot/jobright-proxies.txt`) rather than in
the repository, so the credentials stay out of both git and the process environment; the
pool is read at startup, so a replaced list takes effect on the next restart.

Jobright's own transitions are announced rather than polled for: the first successful
window after startup, entry into failure (with the reason, the address it failed
through, the upstream error, and when the next attempt is due), each egress rotation
naming the address that was refused and the one being tried next, and the recovery with
how many attempts failed and how long the source was degraded. A steady feed, and a
feed failing repeatedly for the same reason, both stay quiet — a channel that repeats
itself every minute stops being read; a changed failure reason is announced again,
because a block turning into a dead proxy is a different problem with a different fix.
The posting runtime sends these messages, including expired-session alerts, through
the bot into the permanent **Core feeds** thread under `DISCORD_LOG_CHANNEL_ID`.
They include the environment label and remain in the application log. Standalone
scraper tools can still use `JOBRIGHT_STATUS_WEBHOOK_URL`; the posting runtime uses
its log thread instead. The standalone webhook is best-effort — an unreachable or
rejecting webhook is logged by host (never by URL, which is itself a credential) and
never interrupts a poll.

The pool applies to Jobright only; every other feed keeps using the host's address. One
address is used until it is refused: a `403`, `429`, timeout, or proxy failure moves the
attempt to the next address within the same poll, while a malformed or empty window —
which would repeat from every address — is not retried elsewhere. Backoff begins only
after every address has been refused, and the listing snapshot fallback uses the
address of the last attempt. Logs and errors carry the proxy host only, never its
credentials. With the variables unset the integration behaves exactly as before.
Continuation rows inherit the preceding company, location cells are split and reduced to
explicit U.S. locations, and every safe application link in a multi-apply row is observed
before global deduplication. A second U.S.-only check runs immediately before Discord
delivery. When a source supplies a usable posting date, observations older than seven
days are rejected before normalization, database persistence, or Discord delivery;
exactly seven days old remains eligible.

## Duplicate guarantees

Duplicate prevention is persistent and happens before Discord delivery:

1. Every upstream row is recorded by `(source_id, external_id)`.
2. Canonical keys cover sanitized application URLs, stable ATS requisition IDs, and a
   normalized company/title/cycle/location signature. URL and requisition identities
   remain location-scoped.
3. Without stable identity evidence, roles merge only when track, normalized company,
   normalized title, cycle, and the complete normalized location set match exactly,
   and their posting dates fall within three days. Near titles such as an internship
   and a co-op remain distinct.
4. Exact fingerprint inserts are conflict-safe, so a duplicate discovered by another
   database connection attaches to the existing canonical job instead of stopping the
   polling run.
5. A transactional outbox permits only one delivery globally per canonical
   role/location, so a channel change cannot replay an already known job.
6. Every embed contains a stable delivery marker. Before a send or retry, the bot
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

To configure a digest role, it also needs **Mention Everyone** in that track's
channel. This permission is checked only when a role ping is enabled; normal job
posts and non-pinging digests do not need it.

Read Message History is required for duplicate reconciliation. The bot fails closed
before publishing if any permission is missing. Message Content Intent is not needed.
Invite the bot with application-command support so the slash command is available.

For the optional category channels, also grant **Create Public Threads** and
**Send Messages in Threads**. These permissions let the bot create each category's
card and thread during startup setup. Category-channel failures are logged
without interrupting primary-channel delivery.

Server administrators can configure either posting channel without restarting:

```text
/set-job-channel track:<Internships|New grad> channel:<channel>
```

The command verifies all four posting permissions, refuses to assign both tracks to
the same channel, persists the setting in Postgres, and safely moves only suppressed
or pending deliveries. Sent or uncertain deliveries are never replayed.

Alternatively, an administrator can create (or reuse) `#internship-jobs` and
`#new-grad-jobs`, then configure both tracks in one command. The bot needs **Manage
Channels** for this setup command:

```text
/create-job-channels category:<optional category>
```

Configure an optional role that is mentioned only once per digest, or omit `role` to
turn the ping off:

```text
/set-job-alert-role track:<Internships|New grad> role:<role>
```

Members can then opt themselves in or out without administrator access:

```text
/job-alert track:<Internships|New grad>
```

The bot must have **Manage Roles**, and its highest role must sit above both opt-in
roles. Role mentions still happen only in the track digest, never on individual job
posts.

Members can also target a company for immediate, track-specific alerts:

```text
/target company:<company> track:<Internships|New grad>
```

Running the same company and track again removes the target. Internship and new-grad
targets are independent. When a matching job is posted, the member is mentioned on
that one duplicate-safe job message; retries and timeout reconciliation cannot create
another ping. Company aliases such as `Palantir Technologies`/`Palantir` and
`NVIDIA AI`/`NVIDIA` share a target. Each member can keep up to 50 company/track
targets, and each company/track alert supports up to 50 members so the Discord message
stays below its content limit.

Members can target a U.S. city/state, a whole state, or U.S.-remote roles separately
for each track:

```text
/target-location track:<Internships|New grad> location:<Boulder, CO>
```

Running the same normalized location and track again removes the target. Equivalent
forms such as `Boulder, CO` and `Boulder, Colorado, United States` share one target;
`Colorado` matches jobs anywhere in the state, and `Remote` matches U.S.-remote jobs.
Ambiguous bare city names are rejected. If a member matches both a company target and
a location target for the same job, the bot mentions them only once on the original
duplicate-safe post. Location targets use the same 50-target-per-member and
50-members-per-target limits as company alerts.

## Configuration

Copy `.env.example` to `.env`. These values are required for production:

```dotenv
BOT_ENV=prod
DISCORD_TOKEN=replace-me
DISCORD_CHANNEL_ID=123456789012345678
DISCORD_NEW_GRAD_CHANNEL_ID=234567890123456789
DISCORD_LOG_CHANNEL_ID=345678901234567890
DATABASE_URL=postgresql://jobbot:jobbot@localhost:5432/jobbot
```

To provide a quieter view of successful internship and new-grad posts, set the
optional category-channel destinations separately from the primary channels.
These are the consolidated development channels; use your own destinations in production:

```dotenv
OUTSOURCE_INTERNSHIP_CHANNEL_ID=1546612310225854504  # intern-consolidated (dev)
OUTSOURCE_NEW_GRAD_CHANNEL_ID=1546612347693699183  # ng-consolidated (dev)
```

Startup creates the full set of nine permanent category cards and attached threads
in the table's order, with **SWE first** and **OTHER last**, even before any listings
arrive. Card and thread names contain only the category, such as **SWE** or **PM**,
with no date. Every later listing goes into its category's existing thread.
Restarts recover the same set and create only missing cards or threads; no daily
setup or rollover is scheduled.

Each card has **Roles that belong here:** followed by its role description.
Permanent cards are recovered from bot-authored Discord history without a date
cutoff. Archived, unlocked threads are reused; locked threads are respected.
Older dated cards remain separate from permanent cards. Primary-channel digests
and subscription pings remain in the primary channels.

| Card | Roles that belong here |
|---|---|
| SWE | Software engineering, frontend, backend, full stack, mobile, embedded software, software systems engineering, platform engineering, SRE, DevOps, and test automation. |
| DATA / AI | Data engineering, data science, machine learning, AI engineering, and applied research. |
| PM | Product management, technical program management, and technical project management. |
| IT / SECURITY | IT support, systems administration, network administration, cybersecurity, security operations, and cloud administration. |
| QUANT | Quantitative research, quantitative trading, and explicitly quantitative developer roles. |
| ANALYST | Business analysts, data analysts, business intelligence, product analysts, and financial analysts. |
| HARDWARE | Electrical engineering, computer hardware, chip design, FPGA, and physical robotics engineering. |
| DESIGN | UX/UI, product design, UX research, and interaction design. |
| OTHER | Roles that don’t clearly fit an existing category or don’t provide enough information to classify confidently. |

Classification happens only at the final mirror step; feeds and ingestion filters
are unchanged. Each job goes to one category, prioritizing explicit title signals
and using descriptions to resolve otherwise unclear roles. Software systems roles
share SWE with backend developers; infrastructure administration belongs to IT /
SECURITY and physical systems to HARDWARE. Company names do not determine categories.

The bot needs View Channel, Send Messages, Embed Links, Read Message History,
Create Public Threads, and Send Messages in Threads in the destination channels.
Original company/location subscription pings and digest role pings stay in the primary
channels. User mention text is retained in copied job embeds' messages, with Discord
mentions disabled so subscribers are not pinged twice. Leave either destination blank
to disable that track's mirror. This remains a best-effort secondary view: a failed
copy is logged and does not retry or change the successful production delivery.
Locked threads are respected; jobs are never dumped into the parent channel as a
fallback. No historical posts are moved or backfilled by this change.

Optional runtime settings:

```dotenv
POLL_INTERVAL_SECONDS=60
DIRECT_ATS_POLL_INTERVAL_SECONDS=900
COMPANY_SOURCE_POLL_INTERVAL_SECONDS=3600
DIGEST_HOUR_LOCAL=16
DIGEST_TIMEZONE=America/New_York
DISCORD_INTERNSHIP_ALERT_ROLE_ID=
DISCORD_NEW_GRAD_ALERT_ROLE_ID=
RECENT_CATCHUP_HOURS=48
REQUEST_TIMEOUT_SECONDS=20
REQUEST_RETRIES=2
SOURCE_CONCURRENCY=6
PUBLISH_DELAY_SECONDS=1.0
LOG_LEVEL=INFO
USER_AGENT=cs-job-poster/2.0
```

`POLL_INTERVAL_SECONDS` has a minimum of 30 seconds; `DIRECT_ATS_POLL_INTERVAL_SECONDS`
and `COMPANY_SOURCE_POLL_INTERVAL_SECONDS` have a minimum of five minutes.
`DIGEST_HOUR_LOCAL` sets the Daily Brief hour (0–23); `DIGEST_TIMEZONE` controls
the brief and the same-day company-listing filter. `America/New_York` follows
EST/EDT automatically. Category cards and threads are permanent and have no daily
rollover. `DIGEST_INTERVAL_SECONDS` is a legacy configuration value and
does not control the active daily schedule. First-party company API
sources run independently of Jobright and default to an hourly collection cycle.
The alert-role environment values are optional and can be overridden live with
`/set-job-alert-role`. `RECENT_CATCHUP_HOURS` is a one-time upgrade bridge: an
already-baselined deployment queues suppressed roles whose source date falls in that
window, while a fresh installation keeps its first core-feed inventory silent. This
upgrade bridge does not make historical company-source roles deliverable. SQLite
remains supported for isolated tests and migration tooling, but the production
publisher requires Postgres so a database advisory lock can prevent competing workers.

## Development and production environments

Set `BOT_ENV=arthur_dev` (or your own `[name]_dev`) locally and `BOT_ENV=prod` in
the VPS env file, then restart the bot. Any environment name is accepted. This
label appears on every Discord scrape status and application log line;
unset values show `UNKNOWN` rather than guessing from the database or channels.
Labels are trimmed and lowercased; `development`/`production` remain aliases for
`dev`/`prod`. Discord displays labels in uppercase.
Status footers identify the host and process so overlapping instances are visible.
Failure titles name the scraper, followed by a plain-language reason. Technical
details below retain the source ID, stage, exception type/message, and code location;
counts are condensed into a run summary. Shared database or publishing failures are labeled separately from an
individual scraper, and preserve counters and errors collected earlier in the run.
Each status gives every configured source its own full-width field, named by feed
and track. Each field name ends with a colon, followed by an indented em dash and its status: fetched (jobs read), checked
but unchanged, skipped because it is not due,
failed, or not run. The configured-source count includes feeds waiting for their
next poll; it is not a count of HTTP requests. IBM appears in the separate company
sources status. Application logs also name every configured core feed and any
feeds skipped until their next poll.
The separate `Run summary` field stays at the bottom and contains totals plus
new and duplicate counts grouped by provider (for example, `Jobright: 12 new · 8 dupes`).
New counts represent newly created jobs after filtering and deduplication; duplicate
counts come from ingestion and exclude rejected jobs. `Jobs read` includes previously
known listings, while queued and posted totals describe delivery. A job appearing
in multiple feeds is credited to the feed that first creates it in the database.
Source rows include `next run in: 3m 20s`, estimated at the log timestamp from the
last recorded check, feed interval, polling cadence, and any Jobright retry
backoff. When scheduling state is unavailable, the countdown says `unknown`.
Core-feed errors remain quiet; company-source failures retain the `@here` alert.
Discord shows up to five bounded error details; application logs retain tracebacks.

Use distinct Discord channels, databases, and preferably separate Discord bot tokens.
The committed templates contain no credentials:

- Development/test server: `deploy/dev.env.example`
- Production VPS: `deploy/prod.env.example` (installed outside the repo as
  `/etc/job-bot/job-bot.env`)

The production service must use Postgres and should only run on the VPS. The local
development setup below uses a dedicated `jobbot_dev` database and is safe for a
test Discord server; it must never point at main-server channels.

## Local development

Python 3.11 is required.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp deploy/dev.env.example .env
make postgres-up
make db-migrate ALEMBIC=".venv/bin/python -m alembic"
make run-postgres
```

For an existing setup, start Docker Desktop and run the combined target from the
repository root:

```bash
make dev-postgres ALEMBIC=".venv/bin/python -m alembic"
```

This starts Postgres, applies migrations, and runs the bot. The module-based Alembic
override also works if the checkout moved and its virtualenv launcher contains an
old absolute path. Once the database is running and migrated, use `make run-postgres`
alone. Stop the bot with Ctrl+C and run that command again after changing code.
Keep Docker and the bot running to receive new jobs. The bot posts to
the channels in your actual `.env`; use development channels when testing.

On the first successful run, all current core inventories are saved with suppressed
job deliveries. Category cards and empty threads initialize independently at startup.
A partial core run cannot complete that shared baseline.
First-party company APIs persist their complete eligible inventories for
deduplication, but only listings whose source-provided posting date is today in the
configured schedule timezone can be delivered. Missing, malformed, and prior-day
posting dates are suppressed, including when a company source is first enabled or its
query scope expands.

Run the test suite with:

```bash
.venv/bin/python -m pytest tests -q
```

GitHub Actions runs the complete suite against Postgres before deployment. There is
no separate Actions publisher: only the long-running service sends messages.

## Operations

Each poll writes structured, count-oriented logs for feed fetch status, accepted and
rejected observations, new canonicals, merges, suppressed/queued deliveries, sent or
reconciled messages, and failures. Upstream errors do not erase prior snapshots or
create closure events. Logging is mandatory for production source scrapers:
`DISCORD_LOG_CHANNEL_ID` must be configured. Startup creates two permanent cards and
threads in that parent channel, in this order:

- **Core feeds** receives core-feed status embeds and Jobright health/session alerts.
- **Direct scrapers** receives company-source status embeds, including IBM and Microsoft.

Threads are reused across days, restarts, and environments sharing a log channel.
Recovery uses bot-authored starter markers without a date cutoff, including archived
threads and interrupted setup. Unlocked archived threads reopen on the next message.
If a thread is locked or unavailable, errors stay in the application log; the bot
does not send the status into the parent channel or create a replacement pair.
Existing parent-channel history stays in place.

Every scrape cycle sends its status embed into the corresponding thread. Successful runs do not mention
anyone. Errors from the first-party `company_sources` scraper group include their
details and send `@here`. Core-feed errors—including Jobright and Simplify—are logged
as warnings without a ping. Explicit Jobright operator alerts retain their configured
user mention. The bot needs View Channel, Send Messages, Embed Links, Read Message
History, Create Public Threads, and Send Messages in Threads in the log channel.
Mention Everyone is also needed for company-source failure pings to notify members.

Existing production tables are preserved. Alembic migration
`20260813_0004_feed_outbox` adds the authoritative observation, key, feed-state, and
delivery tables and seeds existing `discord_posts` rows as sent.

See `deploy/single-vps.md` for the supported single-worker deployment and `TODO.md`
for intentionally deferred commands, process tracking, direct scraping, closures,
post editing, branding, and enrichment.

## Important modules

See [First-Party Company Source Developer Guide](COMPANY_SOURCE_GUIDE.md) for the
complete API discovery, adapter, registration, testing, dedupe, scheduling, and
deployment workflow for adding another company careers API.

- `job_bot/github_feeds.py` — feed definitions, dynamic Jobright windows, conditional
  repository fetching, and schema adapters.
- `job_bot/utils/feed_normalization.py` — location handling, link safety, ATS keys.
- `job_bot/feed_pipeline.py` — concurrent collection, silent baselines, ingestion.
- `job_bot/company_sources/` — first-party company API adapters, registry, runner, and
  independent collection pipeline.
- `job_bot/database.py` — observations, canonical merging, delivery outbox.
- `job_bot/feed_publisher.py` — Discord embeds and marker reconciliation.
- `outsource/categories.py` — role classification and card descriptions.
- `outsource/mirror.py` — permanent category cards, startup setup, and job threads.
- `job_bot/job_channel_commands.py` — persisted admin-only channel configuration.
- `job_bot/mvp_runtime.py` — one-client scheduler, slash commands, and process lock.

Run a read-only live IBM smoke test without a database or Discord connection:

```bash
.venv/bin/python scripts/smoke_ibm_source.py --show 10
```

Run the live IBM source twice through normalization, dedupe, and the configured
development PostgreSQL database without connecting to Discord:

```bash
.venv/bin/python scripts/test_ibm_database_workflow.py --runs 2
```
