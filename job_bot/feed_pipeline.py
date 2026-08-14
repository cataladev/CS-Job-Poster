from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Callable, Iterable, Protocol

from job_bot.database import Database
from job_bot.github_feeds import FEEDS, FeedDefinition, FeedFetchResult, GitHubFeedFetcher
from job_bot.models import AppConfig
from job_bot.utils.feed_normalization import (
    external_identity_keys,
    is_us_location,
    location_scoped_identity_keys,
)
from job_bot.utils.http import AsyncHttpClient
from job_bot.utils.normalization import normalize_job_posting


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
    baseline_completed: bool = False
    errors: list[str] = field(default_factory=list)


HttpClientFactory = Callable[..., AsyncHttpClient]


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
    ) -> None:
        self.config = config
        self.database = database
        self.publisher = publisher
        self.feeds = tuple(feeds)
        self.fetcher = fetcher or GitHubFeedFetcher()
        self.http_client_factory = http_client_factory
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
                source_created = 0
                source_duplicates = 0
                source_merged = 0

                for observation in result.observations:
                    if not observation.locations or not all(
                        is_us_location(
                            location,
                            trusted_us=False,
                            context=observation.application_url,
                        )
                        for location in observation.locations
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
                    if (
                        job.track == "new_grad"
                        and channel_id == self.config.discord_channel_id
                    ):
                        channel_id = None
                    delivery_suppressed = suppress_delivery or channel_id is None
                    ingest = await self.database.ingest_feed_job(
                        job=job,
                        source_id=observation.source_id,
                        external_id=observation.external_id,
                        source_label=observation.source_label,
                        source_url=observation.source_url,
                        source_ref=observation.source_ref,
                        posted_date=observation.posted_date,
                        payload=asdict(observation),
                        canonical_keys=canonical_keys,
                        channel_id=channel_id,
                        suppress_delivery=delivery_suppressed,
                    )
                    if ingest.created:
                        source_created += 1
                        stats.canonical_created += 1
                    else:
                        source_duplicates += 1
                        stats.duplicates += 1
                    if ingest.delivery_state == "pending":
                        stats.deliveries_queued += 1
                    elif ingest.delivery_state == "suppressed":
                        stats.deliveries_suppressed += 1
                    source_merged += len(ingest.merged_canonical_ids)
                    stats.canonicals_merged += len(ingest.merged_canonical_ids)

                    # The database adapter is intentionally synchronous inside its
                    # transaction boundary. Yield between rows so large README
                    # snapshots cannot starve Discord's gateway heartbeat.
                    await asyncio.sleep(0)

                stats.observations += len(result.observations)
                stats.rejected += result.rejected
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
                    "feed_finished source=%s observations=%s rejected=%s created=%s duplicates=%s merged=%s suppressed=%s",
                    feed.source_id,
                    len(result.observations),
                    result.rejected,
                    source_created,
                    source_duplicates,
                    source_merged,
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
                if last_checked_at is not None and (
                    datetime.now(UTC) - last_checked_at
                ).total_seconds() < feed.poll_interval_seconds:
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
