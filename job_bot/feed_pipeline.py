from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable, Protocol

from job_bot.database import Database
from job_bot.github_feeds import FEEDS, FeedDefinition, FeedFetchResult, GitHubFeedFetcher
from job_bot.models import AppConfig
from job_bot.utils.feed_normalization import external_identity_keys, role_signature
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
                    stats.errors.append(error)
                    await self.database.record_feed_error(
                        source_id=feed.source_id,
                        repository=feed.repository,
                        path=feed.path,
                        error=str(result),
                    )
                    self.logger.error("feed_failed source=%s error=%s", feed.source_id, result)
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
                    job = normalize_job_posting(observation.to_job_posting())
                    canonical_keys = {f"url:{job.canonical_url}"}
                    canonical_keys.update(
                        external_identity_keys(
                            job.canonical_url or job.url,
                            company=job.normalized_company,
                        )
                    )
                    canonical_keys.add(
                        role_signature(
                            track=job.track,
                            company=job.normalized_company,
                            title=job.normalized_title,
                            cycle=job.season_term,
                            normalized_locations=job.normalized_locations,
                        )
                    )
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
                        channel_id=self.config.discord_channel_id,
                        suppress_delivery=suppress_delivery,
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
                    feed.source_id for feed in self.feeds
                )
                if stats.baseline_completed:
                    self.logger.info("initial_feed_baseline_completed")

            sent, reconciled = await self.publisher.publish_pending()
            stats.deliveries_sent = sent
            stats.deliveries_reconciled = reconciled
            self.logger.info(
                "feed_run_finished feeds=%s observations=%s rejected=%s created=%s duplicates=%s merged=%s queued=%s suppressed=%s sent=%s reconciled=%s errors=%s",
                stats.feeds_checked,
                stats.observations,
                stats.rejected,
                stats.canonical_created,
                stats.duplicates,
                stats.canonicals_merged,
                stats.deliveries_queued,
                stats.deliveries_suppressed,
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
