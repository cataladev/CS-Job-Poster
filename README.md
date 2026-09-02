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
- Sends one digest per track every four hours by default. Each digest links back to
  the original Discord job posts and can optionally ping a track-specific role.
- Records the first complete inventory silently and posts only later additions.
- Fails closed when a feed is malformed, suspiciously empty, or unavailable.
- Uses conditional GitHub requests (`ETag` and `Last-Modified`) to avoid reprocessing
  unchanged repository feeds. Jobright's dynamic public window is requested
  unconditionally, because the listing page's cache validators can be unchanged while
  the window behind it has moved on; it keeps its own one-minute interval.
- Requires row-level U.S. evidence. Mixed-location rows retain only their U.S.
  locations; foreign, ambiguous, archived, and inactive rows are not published.
- Applies no title, category, seniority, season, or job-age filter.
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
polling every employer board on every cycle and does not use HTML rendering or login-only
endpoints.

First-party company sources live under `job_bot/company_sources/companies/`. They fetch
company-owned careers APIs independently of Jobright, adapt results to the shared
`JobObservation` contract, and enter the same normalization, deduplication, PostgreSQL
outbox, and Discord delivery path. Each source receives a silent first baseline, then
delivers new jobs independently even when a core GitHub or Jobright feed is unavailable.

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
Set `JOBRIGHT_STATUS_WEBHOOK_URL` to a Discord webhook to receive these; unset, the same
lines go to the application log. The webhook is best-effort — an unreachable or
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
DISCORD_TOKEN=replace-me
DISCORD_CHANNEL_ID=123456789012345678
DISCORD_NEW_GRAD_CHANNEL_ID=234567890123456789
DATABASE_URL=postgresql://jobbot:jobbot@localhost:5432/jobbot
```

Optional runtime settings:

```dotenv
POLL_INTERVAL_SECONDS=60
DIRECT_ATS_POLL_INTERVAL_SECONDS=900
COMPANY_SOURCE_POLL_INTERVAL_SECONDS=3600
DIGEST_INTERVAL_SECONDS=14400
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
and `COMPANY_SOURCE_POLL_INTERVAL_SECONDS` have a minimum of five minutes;
`DIGEST_INTERVAL_SECONDS` has a minimum of 15 minutes. First-party company API
sources run independently of Jobright and default to an hourly collection cycle.
The alert-role environment values are optional and can be overridden live with
`/set-job-alert-role`. `RECENT_CATCHUP_HOURS` is a one-time upgrade bridge: an
already-baselined deployment queues suppressed roles whose source date falls in that
window, while a fresh installation keeps its first inventory silent. SQLite remains supported for
isolated tests and migration tooling, but the production publisher requires Postgres
so a database advisory lock can prevent competing workers.

## Development and production environments

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
make db-migrate
make run-postgres
```

On the first successful run, all current core inventories are saved with suppressed
deliveries and nothing is posted. A partial core run cannot complete that shared
baseline. Each first-party company API owns a separate silent first snapshot, so a
failing core feed does not block new jobs from an already-baselined company source.

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
