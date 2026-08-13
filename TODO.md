# Deferred work

The MVP is intentionally a two-channel publisher with one narrowly scoped admin
command. The items below must remain outside the posting runtime until they are
designed as maintainable modules with explicit permissions, tests, and operational
ownership.

## Commands and administration

- Keep `/set-job-channel` isolated in its small command service and retain its
  administrator check, permission validation, persistence, and tests.
- Add high-quality admin-only health and manual-run commands. Include audit logging,
  cooldowns, authorization tests, and clear failure states.
- Add source-health views for stale feeds, schema failures, rejected-row spikes, and
  incomplete baselines.
- Decide whether text commands are worth supporting; prefer an interaction model that
  does not require Message Content Intent.

## Process tracking and watchlists

- Reintroduce private application-process tracking as a separate feature boundary.
- Reintroduce watchlist discovery, requests, approvals, and staff moderation only
  after the publishing path has production evidence behind it.
- Design migrations away from the preserved legacy process/watchlist tables when
  their compatibility window ends.

## Source expansion and scraping

- Add direct ATS and company-careers-page ingestion inspired by Keryx's resilient
  structured-source approach.
- Prefer Greenhouse, Lever, Ashby, Workday, and other machine-readable endpoints over
  rendered-page scraping.
- Give each new feed an explicit schema, health policy, provenance, fixtures, and
  silent first baseline.
- Add guarded browser scraping only where no stable structured source exists.

## Job lifecycle and presentation

- Add confirmed closure and reopen handling using repeated evidence rather than one
  missing or failed snapshot.
- Support post edits only with idempotent delivery state and Discord receipt repair.
- Add verified company branding, logos, compensation normalization, and richer role
  enrichment without guessed domains.
- Add source-health alerts and an operator dashboard.

## Reliability and cleanup

- Add production metrics for fetch latency, feed freshness, canonical merge reasons,
  outbox age, uncertain deliveries, and history-reconciliation duration.
- Exercise crash-after-send and concurrent-worker scenarios against a real Discord
  test channel in a controlled integration environment.
- Remove disconnected legacy command, scraping, branding, and watchlist runtime code
  after its data migration and rollback window are complete.
