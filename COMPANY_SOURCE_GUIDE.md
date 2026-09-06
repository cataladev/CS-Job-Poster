# First-Party Company Source Developer Guide

This is the handoff guide for adding company-owned careers APIs to CS-Job-Poster.
Read it before adding a source. It documents the active runtime, the adapter contract,
the shared normalization/deduplication path, testing, local operation, and deployment.

The short version is:

1. Discover and reproduce the company's public careers API request.
2. Add one adapter under `job_bot/company_sources/companies/`.
3. Convert the company's JSON into `JobObservation` objects.
4. Register the adapter in `job_bot/company_sources/registry.py`.
5. Add fixture-based unit tests and run the shared workflow tests.
6. Run a live smoke test, then a database-only workflow test.
7. Run the long-lived bot to poll hourly and publish genuinely new jobs.

No new database table, Celery worker, Redis instance, or separate Discord bot is
needed for each company.

## 1. What counts as a first-party company source?

A first-party company source calls a public endpoint used by the company's own
careers website. IBM is the reference implementation. Its application links remain
IBM links, rather than links copied from Jobright or a GitHub job list.

This system can also support public ATS endpoints, but the current company-source
folder is primarily for independent company career inventories that need their own
request and response adapter.

Do not attempt to bypass authentication, CAPTCHAs, browser challenges, access
controls, or anti-bot protection. Prefer documented or public endpoints used by the
company's careers page, use bounded polling, and comply with the provider's terms.

## 2. Active architecture

The active process starts here:

```text
bot.py
  -> job_bot/mvp_runtime.py
       -> core FeedPipeline poller
       -> CompanySourcePipeline poller
            -> every registered company adapter (concurrently, with a limit)
            -> shared FeedPipeline.ingest_observations(...)
                 -> validation and normalization
                 -> shared database dedupe and persistence
                 -> Discord delivery outbox
            -> TrackChannelPublisher
                 -> primary internship or new-grad Discord channel
                 -> optional OutsourceMirror, after successful primary delivery
                      -> role category card -> attached job thread
       -> independent daily-category task (startup and 23:59 preparation)
```

Category routing belongs to the final mirror step, not to company adapters. Every
source uses the same role classifier and OTHER fallback. The full set of category
cards and threads is created in a fixed order even without jobs; 23:59 in
`DIGEST_TIMEZONE` prepares tomorrow's set, and startup fills in today's missing cards.
Daily Briefs and company/location subscription pings remain in the primary channels.
See [the category-channel configuration](README.md#configuration) for destinations,
role families, thread permissions, and restart behavior.

The source families differ at collection time and merge at shared ingestion:

| Source family | Collection code | Purpose | Shared after collection? |
| --- | --- | --- | --- |
| Company careers APIs | `job_bot/company_sources/` | Full or bounded first-party company inventories | Yes |
| GitHub lists and Jobright | `job_bot/github_feeds.py` | Aggregated job feeds | Yes |
| Direct ATS link enrichment | `job_bot/ats_feeds.py` and related cache code | Resolve better direct links for known jobs/boards | Yes |

`job_bot/sources/` and the older `pipeline.py` belong to an older architecture. The
current `bot.py` does not use that folder for first-party company collection. Do not
put a new company adapter there.

## 3. Company-source file structure

```text
job_bot/company_sources/
├── __init__.py
├── base.py                     # Adapter protocol and result type
├── registry.py                 # Enabled company adapters
├── runner.py                   # Concurrency, baseline, ingestion, logging
└── companies/
    ├── __init__.py
    ├── ibm.py                  # Reference implementation
    └── <company>.py            # One file per new company

tests/
├── fixtures/company_sources/
│   └── <company>_*.json        # Sanitized deterministic API responses
├── test_company_source_runner.py
└── test_<company>_company_source.py

scripts/
├── smoke_ibm_source.py
└── test_ibm_database_workflow.py
```

There is one shared `base.py`, one shared registry, and one shared runner. Do not
create a base class and registry inside every company folder. Each company normally
needs only its adapter file and tests.

## 4. Adapter contract

Every adapter implements the `CompanySource` protocol in
`job_bot/company_sources/base.py`:

```python
class CompanySource(Protocol):
    source_id: str
    company_name: str
    source_url: str

    def __init__(self, http_client: AsyncHttpClient) -> None: ...

    async def fetch(self) -> CompanySourceResult: ...
```

`fetch()` calls the source API, maps accepted records into `JobObservation` values,
and returns:

```python
CompanySourceResult(
    source_id="company_api_example",
    observations=observations,
    pages_checked=pages_checked,
    rejected=rejected,
    content_sha=content_sha,
)
```

Use the injected async `AsyncHttpClient`. Do not call the blocking `requests`
library inside an async adapter. The shared client supplies timeouts, retry behavior,
and a configured User-Agent. It retries transient responses such as 429 and common
5xx errors; a persistent 403 is an upstream access refusal, not something the
adapter should evade.

### Common `JobObservation` format

The company API can return any JSON field names. The adapter converts them into this
common application format. These are application fields, not a one-to-one copy of
database columns.

| Field | Requirement | Meaning |
| --- | --- | --- |
| `source_id` | Required | Stable namespaced source, e.g. `company_api_ibm` |
| `source_label` | Required | Human label, e.g. `IBM Careers` |
| `source_url` | Required | Human-facing company careers page |
| `source_ref` | Recommended | Trace reference, e.g. `ibm/123456` |
| `external_id` | Required | Stable company job/requisition ID |
| `company` | Required | Canonical company name; normally a class constant |
| `title` | Required | Clean job title |
| `locations` | Required | List of explicit U.S. locations |
| `application_url` | Required | Safe direct company application/job URL |
| `track` | Required | Exactly `internship` or `new_grad` |
| `posted_date` | Required for delivery | Posting date in `YYYY-MM-DD` form; missing or invalid dates are never posted |
| `cycle` | Optional | Season/year such as `Summer 2027`, otherwise `None` |
| `raw_data` | Recommended | Original API record for debugging and traceability |

The adapter owns company-specific interpretation. For example, IBM maps API value
`Entry Level` to `new_grad` and `Internship` to `internship`.

### Adapter responsibilities

An adapter must:

- construct the correct endpoint, method, headers, body, filters, sort, and pagination;
- request software-engineering roles and early-career tracks where the API supports it;
- defensively re-check important categories in the returned records;
- generate a stable external ID from a requisition ID or equivalent;
- produce direct, sanitized company application URLs;
- include only U.S. locations, using the existing location helpers;
- reject malformed or incomplete records instead of inventing identity data;
- parse and validate the source posting date for every deliverable record;
- request only the current day's inventory when the API offers a reliable date filter;
- preserve enough raw source data to debug mapping problems;
- distinguish a valid empty result from an unexpected/malformed response; and
- return a deterministic content hash when practical.

The shared pipeline, not the adapter, owns database insertion, cross-source dedupe,
delivery state, and Discord publishing.

## 5. Finding and reproducing a company careers API

Before writing code:

1. Open the company's careers search page.
2. Open browser developer tools and select the Network tab.
3. Filter to Fetch/XHR traffic.
4. Apply the desired filters in the website: Software Engineering, Internship and/or
   Entry Level, United States, newest first.
5. Find the request that returns job JSON.
6. Record its HTTP method, URL, query parameters, headers, JSON body, pagination,
   response shape, total count, sort field, and filter field names.
7. Reproduce it with Postman or `curl` without relying on a signed-in browser.
8. Identify the stable job ID, title, company location(s), posting date, experience
   level, career area, and direct application URL.
9. Test an empty result and determine how malformed/error payloads differ.

Use server-side filters and newest-first sorting when supported, then keep defensive
client-side validation. When the API exposes a reliable posting-date filter, request
only jobs posted on the current day. Otherwise, stop pagination as soon as the
newest-first results are older than the current day. A bounded inventory response may
still contain older roles for deduplication, but those roles must never be backfilled
to Discord. IBM deliberately requests the newest 60 results in a single page because
that is its agreed bounded scope; the shared runner enforces the delivery date again.

Do not store API keys or tokens in an adapter. If a legitimate source needs a secret,
add a documented environment/config field, keep it out of Git, and pass it into the
runtime securely.

## 6. Implementing a new adapter

Use IBM as the concrete example:

- `job_bot/company_sources/companies/ibm.py`
- `tests/test_ibm_company_source.py`
- `tests/fixtures/company_sources/ibm_*.json`

A simplified skeleton looks like this:

```python
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from job_bot.company_sources.base import CompanySourceResult
from job_bot.github_feeds import JobObservation
from job_bot.utils.feed_normalization import (
    clean_feed_text,
    is_us_location,
    sanitize_application_url,
)
from job_bot.utils.http import AsyncHttpClient


class ExampleResponseError(ValueError):
    pass


class ExampleSource:
    source_id = "company_api_example"
    company_name = "Example"
    source_url = "https://example.com/careers"
    api_url = "https://example.com/api/jobs"

    def __init__(self, http_client: AsyncHttpClient) -> None:
        self.http_client = http_client

    async def fetch(self) -> CompanySourceResult:
        data, _, _ = await self.http_client.get_json(
            self.api_url,
            params={"sort": "newest", "limit": 100},
        )
        raw_jobs = self._read_jobs(data)
        observations: list[JobObservation] = []
        rejected = 0

        for raw_job in raw_jobs:
            observation = self.to_observation(raw_job)
            if observation is None:
                rejected += 1
            else:
                observations.append(observation)

        content = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
        return CompanySourceResult(
            source_id=self.source_id,
            observations=observations,
            pages_checked=1,
            rejected=rejected,
            content_sha=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def _read_jobs(data: object) -> list[Mapping[str, Any]]:
        if not isinstance(data, Mapping) or not isinstance(data.get("jobs"), list):
            raise ExampleResponseError("unexpected Example jobs response")
        jobs = data["jobs"]
        if not all(isinstance(job, Mapping) for job in jobs):
            raise ExampleResponseError("Example jobs must be objects")
        return jobs

    def to_observation(self, raw_job: Mapping[str, Any]) -> JobObservation | None:
        external_id = clean_feed_text(raw_job.get("id"))
        title = clean_feed_text(raw_job.get("title"))
        location = clean_feed_text(raw_job.get("location"))
        application_url = sanitize_application_url(raw_job.get("url")).url
        track = self._track(raw_job.get("experienceLevel"))

        if not external_id or not title or not application_url or not location or not track:
            return None
        if not is_us_location(location, context=application_url):
            return None

        return JobObservation(
            source_id=self.source_id,
            source_label="Example Careers",
            source_url=self.source_url,
            source_ref=f"example/{external_id}",
            external_id=external_id,
            company=self.company_name,
            title=title,
            locations=[location],
            application_url=application_url,
            track=track,
            posted_date=clean_feed_text(raw_job.get("postedDate"))[:10] or None,
            cycle=None,
            raw_data=dict(raw_job),
        )

    @staticmethod
    def _track(value: object) -> str | None:
        return {
            "internship": "internship",
            "entry level": "new_grad",
        }.get(clean_feed_text(value).casefold())
```

This is a template, not a drop-in adapter. Match the real API's response shape and
use its stable fields. Some sources require POST JSON or pagination rather than GET.

## 7. Registering the source

Import the new class and add one registry entry in
`job_bot/company_sources/registry.py`:

```python
from job_bot.company_sources.companies.example import ExampleSource
from job_bot.company_sources.companies.ibm import IBMSource

COMPANY_SOURCE_REGISTRY: dict[str, type[CompanySource]] = {
    "example": ExampleSource,
    "ibm": IBMSource,
}
```

The registry key is the runner key. The source ID stored in feed/database state is
the adapter's `source_id`. Both must remain stable after deployment. Registration is
what makes the existing bot discover and schedule the adapter; no additional
scheduler function is required.

## 8. What happens after the adapter returns observations?

The shared workflow is:

```text
company JSON
  -> company adapter
  -> JobObservation list
  -> common safety/location/age validation
  -> common normalization
  -> shared cross-source dedupe
  -> canonical_jobs + feed_observations + job_variants
  -> discord_deliveries outbox
  -> Discord publisher
```

### Ingestion window versus delivery window

If a usable `posted_date` is more than seven days old, shared ingestion rejects the
record before database insertion and Discord delivery. A job exactly seven days old
is still eligible for storage, but not for company-source delivery. If a source
supplies no valid posting date, the age is unknown, so the shared check cannot reject
it; this is why adapters should parse the date when the API provides one.

That seven-day limit is only a storage/ingestion safety boundary. It does **not** make
a company-source role eligible for Discord. Company-source delivery has a stricter
same-day rule: the posting date must equal the current date in `SCHEDULE_TIMEZONE`.

### Dedupe order and identity

The shared database checks multiple identities, including:

1. the same `(source_id, external_id)`;
2. exact stable canonical URL/ATS identity where available;
3. exact normalized role identity; and
4. fallback exact company + exact title + exact location set + compatible track/cycle,
   with posting dates within three days.

The title comparison is exact after normalization, not fuzzy. Therefore `SWE Intern`
and `SWE Intern Co-op` remain different jobs. Location participates in the fallback;
two otherwise identical postings in different cities can remain separate. IDs from
different sources (for example IBM versus Jobright) do not need to match for the
shared exact role fallback to recognize the same job.

When a duplicate is found, the source observation/variant is retained and the
canonical record's source/last-seen information can be updated. The system does not
create a second canonical job or a second Discord delivery. When no identity matches,
it creates a new canonical job and a delivery entry.

Database uniqueness is also handled conflict-safely: concurrent sources discovering
the same role should converge on the existing canonical instead of crashing on the
unique fingerprint.

### Same-day delivery and source baseline

Each company source owns its own baseline in `feed_state`:

- Every successful run stores current eligible jobs for deduplication, but only rows
  whose source-provided `posted_date` is today in `SCHEDULE_TIMEZONE` may queue Discord
  deliveries. Prior-day, missing, and malformed dates are stored with suppressed
  deliveries.
- This same-day rule also applies to the first run and whenever an existing adapter's
  query scope expands, preventing older inventory from flooding Discord.
- Company sources never backfill prior-day roles to Discord. A suppressed historical
  delivery must not be promoted later merely because the source was disabled, delayed,
  redeployed, or newly registered.
- That run marks only that company source's baseline complete.
- A later run can queue a genuinely new job even if Jobright or a GitHub feed is
  failing or has not completed its own baseline.
- A malformed response or request failure does not complete the source baseline.

`RECENT_CATCHUP_HOURS=0` is recommended for local/dev testing when old suppressed
deliveries should not be promoted. It is separate from normal future delivery.

### Discord routing

The shared pipeline maps:

- `track="internship"` to `DISCORD_CHANNEL_ID`;
- `track="new_grad"` to `DISCORD_NEW_GRAD_CHANNEL_ID`.

The adapter does not send Discord messages directly. The publisher sends only queued
outbox rows. The bot token must belong to a bot that can view and send messages in
the configured channels. Never commit or screenshot `.env` or a bot token.

## 9. Scheduling, concurrency, and failure behavior

The same long-running bot process runs all registered company sources. When the
Discord client is ready, the company poller runs immediately and then schedules the
next run according to:

```dotenv
COMPANY_SOURCE_POLL_INTERVAL_SECONDS=3600
```

The scheduler accounts for elapsed run time, so a run taking five minutes generally
starts the next cycle about 55 minutes later. The configuration enforces a minimum
interval of five minutes.

All company sources are launched concurrently with a semaphore bounded by:

```dotenv
SOURCE_CONCURRENCY=6
```

This setting is currently shared with core feed collection. With 60 one-minute
sources and concurrency 12, the company phase is roughly five one-minute batches,
not 60 sequential minutes. Do not run all sources with unlimited concurrency: the
limit protects sockets, memory, the local process, and upstream services.

Celery and Redis are not currently needed. Async I/O and bounded concurrency are a
good fit for tens or roughly a hundred lightweight HTTP sources. Reconsider a
distributed task queue only when work must span machines, needs independent durable
retry queues, or cannot reliably complete inside the polling window.

One source failure is logged in `source_errors`/`source_runs` and does not prevent
other registered sources from completing. The process-level company run lock also
prevents overlapping company polling cycles.

### Mandatory logging

Logging is part of the company-source contract, not an optional debugging aid. Every
production company-source cycle must produce:

- structured application logs for start, completion, duration, source identity,
  observations, rejected rows, new canonicals, duplicates, queued deliveries,
  suppressed deliveries, sent deliveries, and errors;
- a persisted `source_runs` success or failure record and updated `feed_state`/
  `source_errors` state; and
- one Discord scrape-status embed in the configured log channel.

Production deployments must configure `DISCORD_LOG_CHANNEL_ID` and grant the bot View
Channel, Send Messages, Embed Links, and Mention Everyone in that channel. Successful
runs should not ping anyone; company-source failures must include useful error details
and alert `@here`. Never log tokens, authenticated URLs, proxy credentials, or raw
payload fields that may contain secrets.

## 10. Environment configuration

Keep local values in the ignored `.env`. Start from the appropriate example file.
Do not put real credentials into `deploy/*.env.example`.

Relevant values include:

```dotenv
# Discord runtime
DISCORD_TOKEN=...
DISCORD_CHANNEL_ID=...
DISCORD_NEW_GRAD_CHANNEL_ID=...
DISCORD_LOG_CHANNEL_ID=...

# Local Postgres
POSTGRES_HOST_PORT=5434
DATABASE_URL=postgresql://jobbot:jobbot@localhost:5434/jobbot

# Company-source schedule
COMPANY_SOURCE_POLL_INTERVAL_SECONDS=3600
SOURCE_CONCURRENCY=6

# HTTP behavior
REQUEST_TIMEOUT_SECONDS=20
REQUEST_RETRIES=2
USER_AGENT=...

# Dev delivery behavior
RECENT_CATCHUP_HOURS=0
```

IBM's public endpoint needs no IBM-specific environment value. New public adapters
should not add environment variables unless the source actually requires them.
`POLL_INTERVAL_SECONDS` controls the core feed loop; it does not replace the separate
company-source interval.

## 11. Testing a new source

### Fixture-based adapter tests

Add sanitized JSON fixtures under `tests/fixtures/company_sources/` and test with a
fake async HTTP client. Unit tests must not depend on a live external API.

Cover at least:

- endpoint, method, headers, request filters, newest-first sort, size, and pagination;
- stable external ID extraction;
- title, company, U.S. location, direct link, posted date, and source metadata;
- company experience-level values mapped to `internship`/`new_grad`;
- rejection of senior, wrong-category, non-U.S., unsafe-link, and incomplete records;
- a valid empty inventory;
- malformed/unexpected API responses raising a source-specific error; and
- deterministic page/rejection/content-hash behavior.

IBM reference commands:

```bash
.venv/bin/python -m pytest tests/test_ibm_company_source.py -q
.venv/bin/python -m pytest tests/test_company_source_runner.py -q
.venv/bin/python -m pytest tests -q
```

### Live adapter-only smoke test

IBM includes a read-only smoke script:

```bash
.venv/bin/python scripts/smoke_ibm_source.py --show 10
```

It calls the live API and prints adapted observations. It does not use Postgres or
Discord. Add an equivalent generic/temporary smoke path when implementing a source,
but never make the regular unit suite call the live API.

### Database-only workflow test

Start and migrate local Postgres:

```bash
make postgres-up
make db-migrate ALEMBIC=".venv/bin/python -m alembic"
```

Then run the IBM shared-workflow test:

```bash
.venv/bin/python scripts/test_ibm_database_workflow.py --runs 2
```

This exercises the live IBM adapter, shared normalization, dedupe, feed state, and
database persistence without logging into Discord. The second run should mostly be
duplicates and should not create a second copy of the same jobs.

Do not run `tests/test_postgres_backend.py` against a development database containing
data you care about. That test drops and recreates the `public` schema.

### Full runtime test

With valid dev bot/channel credentials:

```bash
make postgres-up
make db-migrate ALEMBIC=".venv/bin/python -m alembic"
make run-postgres
```

Leave the terminal and Docker running. The process polls company sources immediately
and then hourly. The Mac must remain awake and connected. On the first snapshot, only
roles dated today may be sent; older and undated inventory remains suppressed. To
verify a Discord message, use a controlled same-day fixture/test job or wait for a
genuinely new API posting. Do not weaken the date or dedupe rules in production merely
to force a test message.

Useful log lines include:

```text
CompanySourceRunner company_source_finished ...
CompanySourcePipeline company_source_run_finished ...
FeedDiscordClient feed_poll_scheduled feed_group=company_sources ...
```

Check `created`, `duplicates`, `queued`, `suppressed`, `sent`, and `errors` rather
than assuming every fetched observation should create a Discord post.

## 12. Inspecting Postgres

Open `psql` in the local container:

```bash
docker exec -it job-bot-postgres psql -U jobbot -d jobbot
```

Inspect canonical IBM jobs:

```sql
SELECT
    id,
    track,
    company,
    title,
    location,
    canonical_url,
    source_names,
    first_seen,
    last_seen
FROM canonical_jobs
WHERE lower(company) = 'ibm'
ORDER BY first_seen DESC;
```

Inspect source-specific observations:

```sql
SELECT
    source_id,
    external_id,
    canonical_id,
    posted_date,
    first_seen,
    last_seen
FROM feed_observations
WHERE source_id = 'company_api_ibm'
ORDER BY first_seen DESC;
```

Inspect the source baseline and run history:

```sql
SELECT *
FROM feed_state
WHERE source_id = 'company_api_ibm';

SELECT *
FROM source_runs
WHERE source_name = 'company_api_ibm'
ORDER BY id DESC
LIMIT 10;
```

Inspect delivery states:

```sql
SELECT state, COUNT(*)
FROM discord_deliveries
GROUP BY state
ORDER BY state;
```

The important distinction is:

- `canonical_jobs` is the deduplicated job identity;
- `feed_observations` records what each source observed;
- `job_variants` preserves source/link variants;
- `feed_state` stores source baseline/fetch state;
- `source_runs` and `source_errors` provide operational history; and
- `discord_deliveries` is the durable publishing outbox.

## 13. Deployment

Adding an adapter locally does not update the already-hosted bot automatically.
After review:

1. run adapter, runner, and full tests;
2. review `git diff` and ensure `.env`, tokens, and local data are not staged;
3. commit the adapter, fixtures, tests, registry change, and documentation;
4. push the feature branch and open a pull request;
5. merge through the project's normal review path;
6. deploy/pull the merged code on the existing bot host; and
7. restart the existing service and inspect its company-source logs.

The existing bot process should run the new adapter. Do not create a second Discord
bot or separate hourly service unless the deployment owner intentionally redesigns
the architecture. Production defaults already include the company-source interval,
but the deployment owner should verify environment values and capacity.

The first successful production run for a newly registered source stores its bounded
inventory but may deliver only roles dated today. It must never backfill prior-day or
undated jobs. Confirm the structured application logs, persisted run record, and
Discord status embed after restart.

## 14. New-company checklist

Copy this checklist into the pull request:

```text
[ ] Found a public company-owned careers API request
[ ] Reproduced it outside the browser
[ ] Documented method, endpoint, filters, sort, fields, and pagination
[ ] Confirmed stable job ID and direct company application URL
[ ] Confirmed posting-date and U.S.-location fields
[ ] Limited the API query/pagination to today's jobs where the source supports it
[ ] Added job_bot/company_sources/companies/<company>.py
[ ] Used AsyncHttpClient and bounded pages/results
[ ] Mapped records to the complete JobObservation contract
[ ] Mapped tracks to exactly internship or new_grad
[ ] Rejected wrong category, senior, non-U.S., unsafe, and malformed records
[ ] Added sanitized fixtures and deterministic unit tests
[ ] Registered the adapter in registry.py
[ ] Ran adapter and company runner tests
[ ] Ran the full test suite
[ ] Ran a live adapter-only smoke test
[ ] Ran a local database-only two-pass dedupe test
[ ] Verified same-day jobs queue and prior-day/missing-date jobs stay suppressed
[ ] Verified baseline, source_runs, observations, canonicals, and delivery state
[ ] Verified structured logs and the Discord scrape-status embed
[ ] Confirmed DISCORD_LOG_CHANNEL_ID and log-channel permissions for production
[ ] Confirmed no credentials, .env files, or sensitive payloads are staged
[ ] Coordinated deployment/restart with the existing bot owner
```

## 15. IBM reference behavior

IBM currently demonstrates the complete path:

- POSTs to `https://www-api.ibm.com/search/api/v2`;
- requests Software Engineering, Infrastructure & Technology, Data & Analytics, and
  Cloud career areas;
- requests Internship and Entry Level only;
- sorts by `dcdate` newest first, then score;
- examines at most the first 60 hits;
- uses `field_keyword_19` for location;
- maps `field_keyword_18` to the shared track;
- defaults `company` to `IBM`;
- extracts the external ID from `jobid` (falling back to the hit ID);
- leaves optional `cycle` unset;
- sends accepted observations into the same shared dedupe/database/outbox path as
  every other source; and
- is scheduled by the existing bot once per company-source interval.

When a future source behaves differently, keep the difference inside its adapter and
preserve the common `JobObservation` boundary. That boundary is where all collection
strategies merge into the rest of the application.
