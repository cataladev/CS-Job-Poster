from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlsplit

from job_bot.ats_feeds import build_direct_ats_feeds
from job_bot.database import Database
from job_bot.github_feeds import (
    FEEDS,
    FeedDefinition,
    FeedFetchResult,
    GitHubFeedFetcher,
    JobObservation,
)
from job_bot.models import AppConfig
from job_bot.utils.feed_normalization import (
    external_identity_keys,
    has_explicit_foreign_role_location,
    is_us_location,
    location_scoped_identity_keys,
)
from job_bot.utils.http import AsyncHttpClient
from job_bot.utils.normalization import (
    direct_link_company_key,
    normalize_job_posting,
)


class PendingPublisher(Protocol):
    async def publish_pending(self) -> tuple[int, int]: ...


@dataclass
class FeedRunStats:
    feeds_checked: int = 0
    feeds_unchanged: int = 0
    observations: int = 0
    rejected: int = 0
    canonical_created: int = 0
    duplicates: int = 0
    canonicals_merged: int = 0
    deliveries_queued: int = 0
    deliveries_suppressed: int = 0
    deliveries_promoted: int = 0
    deliveries_sent: int = 0
    deliveries_reconciled: int = 0
    direct_link_boards_fetched: int = 0
    baseline_completed: bool = False
    errors: list[str] = field(default_factory=list)


@dataclass
class ObservationIngestStats:
    observations: int = 0
    rejected: int = 0
    canonical_created: int = 0
    duplicates: int = 0
    canonicals_merged: int = 0
    deliveries_queued: int = 0
    deliveries_suppressed: int = 0
    errors: list[str] = field(default_factory=list)


HttpClientFactory = Callable[..., AsyncHttpClient]
DIRECT_ATS_CACHE_TTL = timedelta(hours=4)
MAX_POSTING_AGE_DAYS = 7

# A feed's last check is stamped when its fetch finishes, a few seconds into the run,
# but the loop starts each run one full interval after the previous one *started*. A
# feed whose interval equals the loop's would therefore always be short by the run
# duration and fire every other tick. This slack absorbs that, so an interval means
# the cadence it says rather than "the next tick after it".
FEED_INTERVAL_SLACK_SECONDS = 30


def _is_jobright_observation(observation: object, application_url: str) -> bool:
    source_id = str(getattr(observation, "source_id", "")).casefold()
    host = (urlsplit(application_url).hostname or "").casefold()
    return source_id.startswith("jobright") or host in {"jobright.ai", "www.jobright.ai"}


def _posting_is_too_old(posted_date: str | None) -> bool:
    """Reject known posting dates older than the inclusive seven-day window."""
    raw = str(posted_date or "").strip()
    if not raw:
        return False
    try:
        posting_day = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return False
    return (datetime.now(UTC).date() - posting_day).days > MAX_POSTING_AGE_DAYS


class FeedPipeline:
    def __init__(
        self,
        *,
        config: AppConfig,
        database: Database,
        publisher: PendingPublisher,
        feeds: Iterable[FeedDefinition] = FEEDS,
        fetcher: GitHubFeedFetcher | None = None,
        http_client_factory: HttpClientFactory = AsyncHttpClient,
        direct_link_feeds: Iterable[FeedDefinition] | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.publisher = publisher
        self.feeds = tuple(feeds)
        self.fetcher = fetcher or GitHubFeedFetcher()
        self.http_client_factory = http_client_factory
        direct_feeds = (
            tuple(direct_link_feeds)
            if direct_link_feeds is not None
            else build_direct_ats_feeds(
                config.company_seed_path,
                poll_interval_seconds=int(DIRECT_ATS_CACHE_TTL.total_seconds()),
            )
        )
        self._direct_link_feeds_by_company: dict[str, list[FeedDefinition]] = {}
        for feed in direct_feeds:
            normalized_company = direct_link_company_key(feed.company_name or "")
            if normalized_company:
                self._direct_link_feeds_by_company.setdefault(normalized_company, []).append(feed)
        # Some Workday tenants reject bursts of anonymous CXS requests. The
        # cache path already runs at most once per board every four hours, so
        # serialize only that provider instead of slowing normal feed polling.
        self._workday_fetch_lock = asyncio.Lock()
        self.logger = logging.getLogger(self.__class__.__name__)
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def run(self) -> FeedRunStats:
        if self._lock.locked():
            stats = FeedRunStats()
            stats.errors.append("feed run already in progress")
            return stats

        async with self._lock:
            stats = FeedRunStats()
            initial_baseline_complete = await self.database.is_initial_feed_baseline_complete()
            self.logger.info(
                "feed_run_started feeds=%s initial_baseline_complete=%s",
                len(self.feeds),
                initial_baseline_complete,
            )
            async with self.http_client_factory(
                timeout_seconds=self.config.request_timeout_seconds,
                retries=self.config.request_retries,
                user_agent=self.config.user_agent,
            ) as http_client:
                semaphore = asyncio.Semaphore(max(1, self.config.source_concurrency))
                tasks = [
                    self._fetch_one(feed, http_client, semaphore)
                    for feed in self.feeds
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                stats.direct_link_boards_fetched = await self._prime_direct_link_cache(
                    results,
                    http_client,
                    semaphore,
                )

            for feed, result in zip(self.feeds, results):
                stats.feeds_checked += 1
                if isinstance(result, BaseException):
                    error = f"{feed.source_id}: {result}"
                    if feed.required_for_baseline:
                        stats.errors.append(error)
                    await self.database.record_feed_error(
                        source_id=feed.source_id,
                        repository=feed.repository,
                        path=feed.path,
                        error=str(result),
                    )
                    self.logger.error("feed_failed source=%s error=%s", feed.source_id, result)
                    continue

                if result.skipped:
                    self.logger.debug("feed_not_due source=%s", feed.source_id)
                    continue

                if result.not_modified:
                    stats.feeds_unchanged += 1
                    await self.database.record_feed_not_modified(feed.source_id)
                    self.logger.info("feed_unchanged source=%s", feed.source_id)
                    continue

                state = await self.database.get_feed_state(feed.source_id)
                feed_baselined = bool(
                    state
                    and state["baseline_complete"]
                    and state["repository"] == feed.repository
                    and state["path"] == feed.path
                )
                suppress_delivery = not initial_baseline_complete or not feed_baselined
                ingest_stats = await self.ingest_observations(
                    result.observations,
                    suppress_delivery=suppress_delivery,
                )
                stats.observations += ingest_stats.observations
                stats.rejected += ingest_stats.rejected
                stats.canonical_created += ingest_stats.canonical_created
                stats.duplicates += ingest_stats.duplicates
                stats.canonicals_merged += ingest_stats.canonicals_merged
                stats.deliveries_queued += ingest_stats.deliveries_queued
                stats.deliveries_suppressed += ingest_stats.deliveries_suppressed
                stats.rejected += result.rejected
                source_ingest_errors = ingest_stats.errors
                if source_ingest_errors:
                    error = (
                        f"{len(source_ingest_errors)} observation ingestion error(s); "
                        f"first: {source_ingest_errors[0]}"
                    )
                    if feed.required_for_baseline:
                        stats.errors.append(f"{feed.source_id}: {error}")
                    await self.database.record_feed_error(
                        source_id=feed.source_id,
                        repository=feed.repository,
                        path=feed.path,
                        error=error,
                    )
                else:
                    await self.database.record_feed_success(
                        source_id=feed.source_id,
                        repository=feed.repository,
                        path=feed.path,
                        etag=result.etag,
                        content_sha=result.content_sha,
                        last_modified=result.last_modified,
                        baseline_complete=True,
                    )
                self.logger.info(
                    "feed_finished source=%s observations=%s rejected=%s created=%s "
                    "duplicates=%s merged=%s ingest_errors=%s suppressed=%s",
                    feed.source_id,
                    len(result.observations),
                    result.rejected,
                    ingest_stats.canonical_created,
                    ingest_stats.duplicates,
                    ingest_stats.canonicals_merged,
                    len(source_ingest_errors),
                    suppress_delivery,
                )

            if not initial_baseline_complete and not stats.errors:
                stats.baseline_completed = await self.database.complete_initial_feed_baseline_if_ready(
                    feed.source_id for feed in self.feeds if feed.required_for_baseline
                )
                if stats.baseline_completed:
                    await self.database.mark_recent_suppressed_catchup_complete()
                    self.logger.info("initial_feed_baseline_completed")

            if initial_baseline_complete and not stats.errors:
                cutoff_date = (
                    datetime.now(UTC) - timedelta(hours=self.config.recent_catchup_hours)
                ).date().isoformat()
                stats.deliveries_promoted = (
                    await self.database.promote_recent_suppressed_deliveries_once(
                        cutoff_date,
                        (
                            (feed.source_id, f"{feed.branch}/{feed.path}")
                            for feed in self.feeds
                        ),
                    )
                    if self.config.recent_catchup_hours > 0
                    else 0
                )
                if self.config.recent_catchup_hours == 0:
                    await self.database.mark_recent_suppressed_catchup_complete()
                if stats.deliveries_promoted:
                    self.logger.info(
                        "recent_baseline_deliveries_promoted count=%s cutoff_date=%s",
                        stats.deliveries_promoted,
                        cutoff_date,
                    )

            sent, reconciled = await self.publisher.publish_pending()
            stats.deliveries_sent = sent
            stats.deliveries_reconciled = reconciled
            self.logger.info(
                "feed_run_finished feeds=%s observations=%s rejected=%s created=%s duplicates=%s merged=%s queued=%s suppressed=%s promoted=%s sent=%s reconciled=%s errors=%s",
                stats.feeds_checked,
                stats.observations,
                stats.rejected,
                stats.canonical_created,
                stats.duplicates,
                stats.canonicals_merged,
                stats.deliveries_queued,
                stats.deliveries_suppressed,
                stats.deliveries_promoted,
                stats.deliveries_sent,
                stats.deliveries_reconciled,
                len(stats.errors),
            )
            return stats

    async def ingest_observations(
        self,
        observations: Iterable[JobObservation],
        *,
        suppress_delivery: bool,
    ) -> ObservationIngestStats:
        """Normalize and persist observations through the shared dedupe boundary."""

        batch = tuple(observations)
        stats = ObservationIngestStats(observations=len(batch))
        for observation in batch:
            if _posting_is_too_old(observation.posted_date):
                stats.rejected += 1
                self.logger.info(
                    "observation_rejected_stale source=%s external_id=%s posted_date=%s max_age_days=%s",
                    observation.source_id,
                    observation.external_id,
                    observation.posted_date,
                    MAX_POSTING_AGE_DAYS,
                )
                continue

            if (
                has_explicit_foreign_role_location(observation.title)
                or not observation.locations
                or not all(
                    is_us_location(
                        location,
                        trusted_us=False,
                        context=observation.application_url,
                    )
                    for location in observation.locations
                )
            ):
                stats.rejected += 1
                self.logger.warning(
                    "observation_rejected_non_us source=%s external_id=%s locations=%s",
                    observation.source_id,
                    observation.external_id,
                    observation.locations,
                )
                continue

            job = normalize_job_posting(observation.to_job_posting())
            is_jobright = _is_jobright_observation(
                observation,
                job.canonical_url or job.url,
            )
            resolved_direct_url = None
            if is_jobright:
                jobright_url = job.canonical_url or job.url
                resolved_direct_url = await self.database.find_cached_direct_link(
                    job=job,
                    posted_date=observation.posted_date,
                )
                if resolved_direct_url:
                    job.url = resolved_direct_url
                    job.canonical_url = resolved_direct_url
                    job.alternate_urls = [*job.alternate_urls, jobright_url]
                    job.raw_data = {
                        **job.raw_data,
                        "jobright_fallback_url": jobright_url,
                        "direct_link_cache": True,
                    }
                    job = normalize_job_posting(job)

            if job.normalized_company in self.config.company_blacklist:
                stats.rejected += 1
                self.logger.info(
                    "observation_rejected_company_blacklist source=%s external_id=%s company=%s",
                    observation.source_id,
                    observation.external_id,
                    job.company,
                )
                continue

            exact_identity_keys = {f"url:{job.canonical_url}"}
            exact_identity_keys.update(
                external_identity_keys(
                    job.canonical_url or job.url,
                    company=job.normalized_company,
                )
            )
            canonical_keys = location_scoped_identity_keys(
                exact_identity_keys,
                normalized_locations=job.normalized_locations,
            )
            channel_id = self.config.channel_id_for_track(job.track)
            if job.track == "new_grad" and channel_id == self.config.discord_channel_id:
                channel_id = None
            delivery_suppressed = suppress_delivery or channel_id is None
            payload = asdict(observation)
            payload["raw_data"] = dict(job.raw_data)
            try:
                ingest = await self.database.ingest_feed_job(
                    job=job,
                    source_id=observation.source_id,
                    external_id=observation.external_id,
                    source_label=observation.source_label,
                    source_url=observation.source_url,
                    source_ref=observation.source_ref,
                    posted_date=observation.posted_date,
                    payload=payload,
                    canonical_keys=canonical_keys,
                    channel_id=channel_id,
                    suppress_delivery=delivery_suppressed,
                )
            except Exception as exc:
                stats.errors.append(f"external_id={observation.external_id}: {exc}")
                self.logger.exception(
                    "observation_ingest_failed source=%s external_id=%s",
                    observation.source_id,
                    observation.external_id,
                )
                # The transaction was rolled back. Keep processing other rows,
                # but let callers record a failed snapshot rather than baseline it.
                await asyncio.sleep(0)
                continue
            if not is_jobright:
                await self.database.cache_direct_link(
                    job=job,
                    source_id=observation.source_id,
                    source_ref=observation.source_ref,
                    posted_date=observation.posted_date,
                )
            if ingest.created:
                stats.canonical_created += 1
            else:
                stats.duplicates += 1
            if ingest.delivery_state == "pending":
                stats.deliveries_queued += 1
            elif ingest.delivery_state == "suppressed":
                stats.deliveries_suppressed += 1
            stats.canonicals_merged += len(ingest.merged_canonical_ids)

            # The database adapter is intentionally synchronous inside its
            # transaction boundary. Yield between rows so large snapshots cannot
            # starve Discord's gateway heartbeat.
            await asyncio.sleep(0)
        return stats

    async def _prime_direct_link_cache(
        self,
        results: list[FeedFetchResult | BaseException],
        http_client: AsyncHttpClient,
        semaphore: asyncio.Semaphore,
    ) -> int:
        """Fetch only stale ATS boards for companies in the current Jobright batch.

        Responses populate the local direct-link cache only; they are not ingested
        as standalone delivery sources, so this cannot create duplicate Discord
        messages.
        """
        if not self._direct_link_feeds_by_company:
            return 0
        target_feeds: dict[str, FeedDefinition] = {}
        targeted_queries: dict[str, set[str]] = {}
        for result in results:
            if not isinstance(result, FeedFetchResult) or not result.definition.source_id.startswith(
                "jobright"
            ):
                continue
            for observation in result.observations:
                for feed in self._direct_link_feeds_by_company.get(
                    direct_link_company_key(observation.company), []
                ):
                    target_feeds[feed.source_id] = feed
                    if feed.provider in {"workday", "bytedance", "amazon"}:
                        targeted_queries.setdefault(feed.source_id, set()).add(observation.title)
        due_source_ids = await self.database.get_due_direct_ats_sources(
            target_feeds,
            stale_before=(datetime.now(UTC) - DIRECT_ATS_CACHE_TTL).isoformat(),
        )
        if not due_source_ids:
            return 0
        due_feeds = [target_feeds[source_id] for source_id in sorted(due_source_ids)]
        fetched = await asyncio.gather(
            *(
                self._fetch_direct_link_board(
                    feed,
                    http_client,
                    semaphore,
                    search_texts=targeted_queries.get(feed.source_id, ()),
                )
                for feed in due_feeds
            ),
            return_exceptions=True,
        )
        successful = 0
        for feed, result in zip(due_feeds, fetched):
            if isinstance(result, BaseException):
                await self.database.record_direct_ats_cache_check(
                    source_id=feed.source_id,
                    provider=feed.provider or "unknown",
                    board_token=feed.path,
                    error=str(result),
                )
                self.logger.warning(
                    "direct_link_board_failed source=%s error=%s", feed.source_id, result
                )
                continue
            for observation in result.observations:
                await self.database.cache_direct_link(
                    job=normalize_job_posting(observation.to_job_posting()),
                    source_id=observation.source_id,
                    source_ref=observation.source_ref,
                    posted_date=observation.posted_date,
                )
            await self.database.record_direct_ats_cache_check(
                source_id=feed.source_id,
                provider=feed.provider or "unknown",
                board_token=feed.path,
            )
            successful += 1
        return successful

    async def _fetch_direct_link_board(
        self,
        feed: FeedDefinition,
        http_client: AsyncHttpClient,
        semaphore: asyncio.Semaphore,
        *,
        search_texts: Iterable[str] = (),
    ) -> FeedFetchResult:
        async with semaphore:
            if feed.provider == "workday":
                async with self._workday_fetch_lock:
                    if search_texts:
                        return await self.fetcher.fetch_workday_queries(
                            feed, http_client, search_texts=search_texts
                        )
                    return await self.fetcher.fetch(feed, http_client)
            if feed.provider == "bytedance" and search_texts:
                return await self.fetcher.fetch_bytedance_queries(
                    feed, http_client, search_texts=tuple(search_texts)
                )
            if feed.provider == "amazon" and search_texts:
                return await self.fetcher.fetch_amazon_queries(
                    feed, http_client, search_texts=tuple(search_texts)
                )
            return await self.fetcher.fetch(feed, http_client)

    async def _fetch_one(
        self,
        feed: FeedDefinition,
        http_client: AsyncHttpClient,
        semaphore: asyncio.Semaphore,
    ) -> FeedFetchResult:
        async with semaphore:
            state = await self.database.get_feed_state(feed.source_id)
            if feed.poll_interval_seconds and state:
                last_checked = state.get("last_checked_at")
                try:
                    last_checked_at = datetime.fromisoformat(str(last_checked))
                    if last_checked_at.tzinfo is None:
                        last_checked_at = last_checked_at.replace(tzinfo=UTC)
                except (TypeError, ValueError):
                    last_checked_at = None
                due_after = max(
                    0,
                    feed.poll_interval_seconds - FEED_INTERVAL_SLACK_SECONDS,
                )
                if last_checked_at is not None and (
                    datetime.now(UTC) - last_checked_at
                ).total_seconds() < due_after:
                    return FeedFetchResult(
                        definition=feed,
                        observations=[],
                        rejected=0,
                        etag=state.get("etag"),
                        last_modified=state.get("last_modified"),
                        content_sha=None,
                        skipped=True,
                    )
            state_matches_feed = bool(
                state
                and state["repository"] == feed.repository
                and state["path"] == feed.path
            )
            return await self.fetcher.fetch(
                feed,
                http_client,
                etag=state["etag"] if state_matches_feed else None,
                last_modified=state["last_modified"] if state_matches_feed else None,
            )
