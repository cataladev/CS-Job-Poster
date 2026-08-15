# CS Job Poster

A focused Discord publisher for curated U.S. internships and new-grad roles.

The bot polls ten U.S. aggregator surfaces (eight from five GitHub repositories and two
public Jobright listings) plus direct, public employer ATS boards. It combines every
successful snapshot, removes duplicates across sources, and publishes newly added roles
to separate internship and new-grad Discord channels. Its MVP commands configure posting
channels and optional digest-alert roles.

## Runtime behavior

- Polls every five minutes by default.
- Runs the GitHub and Jobright core feeds in their own five-minute loop, so a slow
  direct-employer sweep cannot delay new internship or new-grad Discord posts.
- Derives 309 unique direct employer boards from the bundled company seed: Greenhouse,
  Lever, Ashby, and SmartRecruiters. Those boards use their public structured endpoints
  and are polled every 15 minutes by default, independently of the main poll cadence.
- Direct boards are filtered to internship and new-grad signals before ingestion; a
  board with no matching roles is a healthy empty result, not a failed source.
- Posts each newly discovered role immediately to its configured track channel,
  without a role mention.
- Sends one digest per track every four hours by default. Each digest links back to
  the original Discord job posts and can optionally ping a track-specific role.
- Records the first complete inventory silently and posts only later additions.
- Fails closed when a feed is malformed, suspiciously empty, or unavailable.
- Uses conditional GitHub requests (`ETag` and `Last-Modified`) to avoid reprocessing
  unchanged repository feeds; Jobright's dynamic public windows are intentionally
  checked each main poll.
- Requires row-level U.S. evidence. Mixed-location rows retain only their U.S.
  locations; foreign, ambiguous, archived, and inactive rows are not published.
- Applies no title, category, seniority, season, or job-age filter.
- Sends internships to `DISCORD_CHANNEL_ID` and new-grad roles to
  `DISCORD_NEW_GRAD_CHANNEL_ID`.
- Suppresses new-grad delivery when its channel is unset or matches the internship
  channel; it never falls back to posting new-grad roles with internships.
- Never treats an upstream edit to an existing source row as a new job.

The executable entrypoint is `bot.py`. It starts a minimal `discord.Client` without
Message Content Intent and exposes two admin-only slash commands. The legacy text
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
| Jobright software-engineering internships | `jobright.ai/jobs/software-engineer-intern-jobs-in-united-states` | Public SSR query + anonymous visitor API |
| Jobright software-engineering new grad | `jobright.ai/jobs/software-engineer-new-graduate-jobs-in-united-states` | Public SSR query + anonymous visitor API |

In addition, the active runtime builds a reviewed direct-employer board index from the
supported ATS URLs in `data/company_watchlist_seed.csv`: currently Greenhouse, Lever,
Ashby, and SmartRecruiters boards. When a Jobright role's company has a matching board,
the runtime requests that public API only if its local result is more than four hours old,
then uses an unambiguous direct match while retaining Jobright as fallback. This avoids
polling every employer board on every cycle and does not use HTML rendering or login-only
endpoints.

The fixed feed definitions live in `job_bot/github_feeds.py`. `config/sources.yaml`
is deliberately not a runtime switchboard for the MVP. The Jobright integration derives
its public U.S. search query from the server-rendered listing, then merges the first 100
recommended and first 100 most-recent anonymous visitor results. This bounded scan is
checked every main poll, avoids account-specific recommendations and unbounded scraping,
and falls back to the public listing snapshot if the visitor API is temporarily down.
Continuation rows inherit the preceding company, location cells are split and reduced to
explicit U.S. locations, and every safe application link in a multi-apply row is observed
before global deduplication. A second U.S.-only check runs immediately before Discord
delivery.

## Duplicate guarantees

Duplicate prevention is persistent and happens before Discord delivery:

1. Every upstream row is recorded by `(source_id, external_id)`.
2. Canonical keys cover sanitized application URLs, stable ATS requisition IDs, and a
   normalized company/title/cycle/location signature. URL and requisition identities
   are location-scoped.
3. Matching company/title/cycle roles merge whenever their normalized locations
   overlap. Disjoint locations remain distinct, even when repositories reuse the same
   application URL or requisition ID.
4. A transactional outbox permits only one delivery globally per canonical
   role/location, so a channel change cannot replay an already known job.
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
POLL_INTERVAL_SECONDS=300
DIRECT_ATS_POLL_INTERVAL_SECONDS=900
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
has a minimum of five minutes; `DIGEST_INTERVAL_SECONDS` has a minimum of 15 minutes.
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

On the first successful run, all ten current inventories are saved with suppressed
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

- `job_bot/github_feeds.py` — feed definitions, dynamic Jobright windows, conditional
  repository fetching, and schema adapters.
- `job_bot/utils/feed_normalization.py` — location handling, link safety, ATS keys.
- `job_bot/feed_pipeline.py` — concurrent collection, silent baselines, ingestion.
- `job_bot/database.py` — observations, canonical merging, delivery outbox.
- `job_bot/feed_publisher.py` — Discord embeds and marker reconciliation.
- `job_bot/job_channel_commands.py` — persisted admin-only channel configuration.
- `job_bot/mvp_runtime.py` — one-client scheduler, slash commands, and process lock.
