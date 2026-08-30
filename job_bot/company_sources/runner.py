from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from job_bot.company_sources.registry import (
    COMPANY_SOURCE_REGISTRY,
    create_company_source,
)
from job_bot.database import Database
from job_bot.feed_pipeline import FeedPipeline, ObservationIngestStats
from job_bot.models import AppConfig
from job_bot.utils.http import AsyncHttpClient

HttpClientFactory = Callable[..., AsyncHttpClient]


@dataclass
class CompanySourceRunStats:
    source_key: str
    source_id: str = ""
    pages_checked: int = 0
    observations: int = 0
    rejected: int = 0
    canonical_created: int = 0
    duplicates: int = 0
    canonicals_merged: int = 0
    deliveries_queued: int = 0
    deliveries_suppressed: int = 0
    baseline_completed: bool = False
    skipped: bool = False

    def add_ingest(self, ingest: ObservationIngestStats) -> None:
        self.observations += ingest.observations
        self.rejected += ingest.rejected
        self.canonical_created += ingest.canonical_created
        self.duplicates += ingest.duplicates
        self.canonicals_merged += ingest.canonicals_merged
        self.deliveries_queued += ingest.deliveries_queued
        self.deliveries_suppressed += ingest.deliveries_suppressed


@dataclass
class CompanySourcePipelineStats:
    sources_checked: int = 0
    sources_skipped: int = 0
    observations: int = 0
    rejected: int = 0
    canonical_created: int = 0
    duplicates: int = 0
    canonicals_merged: int = 0
    deliveries_queued: int = 0
    deliveries_suppressed: int = 0
    deliveries_sent: int = 0
    deliveries_reconciled: int = 0
    baselines_completed: int = 0
    errors: list[str] = field(default_factory=list)

    def add_run(self, run: CompanySourceRunStats) -> None:
        self.sources_checked += 1
        self.sources_skipped += int(run.skipped)
        self.observations += run.observations
        self.rejected += run.rejected
        self.canonical_created += run.canonical_created
        self.duplicates += run.duplicates
        self.canonicals_merged += run.canonicals_merged
        self.deliveries_queued += run.deliveries_queued
        self.deliveries_suppressed += run.deliveries_suppressed
        self.baselines_completed += int(run.baseline_completed)


class CompanySourceRunner:
    """Run one first-party source through shared ingestion and feed state."""

    def __init__(
        self,
        *,
        config: AppConfig,
        database: Database,
        pipeline: FeedPipeline,
    ) -> None:
        self.config = config
        self.database = database
        self.pipeline = pipeline
        self.logger = logging.getLogger(self.__class__.__name__)
        self._source_locks: dict[str, asyncio.Lock] = {}

    async def run(
        self,
        source_key: str,
        http_client: AsyncHttpClient,
    ) -> CompanySourceRunStats:
        lock = self._source_locks.setdefault(source_key, asyncio.Lock())
        if lock.locked():
            self.logger.warning(
                "company_source_already_running source_key=%s", source_key
            )
            return CompanySourceRunStats(source_key=source_key, skipped=True)

        async with lock:
            source = create_company_source(source_key, http_client)
            source_repository = f"company-api/{source_key}"
            source_path = source.source_url
            stats = CompanySourceRunStats(
                source_key=source_key, source_id=source.source_id
            )
            try:
                state = await self.database.get_feed_state(source.source_id)
                source_baselined = bool(
                    state
                    and state["baseline_complete"]
                    and state["repository"] == source_repository
                    and state["path"] == source_path
                )
                # Company APIs own their baseline lifecycle. A failing GitHub or
                # Jobright feed must not prevent a healthy first-party source
                # from delivering jobs discovered after its silent snapshot.
                suppress_delivery = not source_baselined

                result = await source.fetch()
                stats.pages_checked = result.pages_checked
                stats.rejected += result.rejected
                ingest = await self.pipeline.ingest_observations(
                    result.observations,
                    suppress_delivery=suppress_delivery,
                )
                stats.add_ingest(ingest)
                if ingest.errors:
                    raise RuntimeError(
                        f"{len(ingest.errors)} observation ingestion error(s); "
                        f"first: {ingest.errors[0]}"
                    )
                await self.database.record_feed_success(
                    source_id=source.source_id,
                    repository=source_repository,
                    path=source_path,
                    etag=None,
                    content_sha=result.content_sha,
                    last_modified=None,
                    baseline_complete=True,
                )
                await self.database.record_source_run(
                    source_name=source.source_id,
                    track="company_api",
                    status="success",
                    jobs_fetched=stats.observations,
                    jobs_filtered=stats.rejected,
                    jobs_new=stats.canonical_created,
                    jobs_duplicates=stats.duplicates,
                    metadata={
                        "source_key": source_key,
                        "pages_checked": stats.pages_checked,
                        "canonicals_merged": stats.canonicals_merged,
                        "deliveries_queued": stats.deliveries_queued,
                        "deliveries_suppressed": stats.deliveries_suppressed,
                    },
                )
                stats.baseline_completed = not source_baselined
                self.logger.info(
                    "company_source_finished source=%s pages=%s observations=%s rejected=%s "
                    "created=%s duplicates=%s suppressed=%s baseline_completed=%s",
                    source.source_id,
                    stats.pages_checked,
                    stats.observations,
                    stats.rejected,
                    stats.canonical_created,
                    stats.duplicates,
                    stats.deliveries_suppressed,
                    stats.baseline_completed,
                )
                return stats
            except Exception as exc:
                await self.database.record_feed_error(
                    source_id=source.source_id,
                    repository=source_repository,
                    path=source_path,
                    error=str(exc),
                )
                await self.database.record_source_run(
                    source_name=source.source_id,
                    track="company_api",
                    status="error",
                    jobs_fetched=stats.observations,
                    jobs_filtered=stats.rejected,
                    jobs_new=stats.canonical_created,
                    jobs_duplicates=stats.duplicates,
                    metadata={
                        "source_key": source_key,
                        "pages_checked": stats.pages_checked,
                    },
                    error=str(exc),
                )
                self.logger.exception(
                    "company_source_failed source=%s", source.source_id
                )
                raise


class CompanySourcePipeline:
    """Collect all registered company APIs with bounded concurrency."""

    def __init__(
        self,
        *,
        config: AppConfig,
        database: Database,
        feed_pipeline: FeedPipeline,
        source_keys: Iterable[str] | None = None,
        http_client_factory: HttpClientFactory = AsyncHttpClient,
    ) -> None:
        self.config = config
        self.feed_pipeline = feed_pipeline
        self.source_keys = tuple(source_keys or COMPANY_SOURCE_REGISTRY)
        self.http_client_factory = http_client_factory
        self.runner = CompanySourceRunner(
            config=config,
            database=database,
            pipeline=feed_pipeline,
        )
        self.logger = logging.getLogger(self.__class__.__name__)
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def run(self) -> CompanySourcePipelineStats:
        if self._lock.locked():
            stats = CompanySourcePipelineStats(sources_skipped=len(self.source_keys))
            stats.errors.append("company source run already in progress")
            return stats

        async with self._lock:
            stats = CompanySourcePipelineStats()
            semaphore = asyncio.Semaphore(max(1, self.config.source_concurrency))
            async with self.http_client_factory(
                timeout_seconds=self.config.request_timeout_seconds,
                retries=self.config.request_retries,
                user_agent=self.config.user_agent,
            ) as http_client:

                async def run_one(source_key: str) -> CompanySourceRunStats:
                    async with semaphore:
                        return await self.runner.run(source_key, http_client)

                results = await asyncio.gather(
                    *(run_one(source_key) for source_key in self.source_keys),
                    return_exceptions=True,
                )

            for source_key, result in zip(self.source_keys, results):
                if isinstance(result, BaseException):
                    stats.sources_checked += 1
                    stats.errors.append(f"{source_key}: {result}")
                    continue
                stats.add_run(result)

            sent, reconciled = await self.feed_pipeline.publisher.publish_pending()
            stats.deliveries_sent = sent
            stats.deliveries_reconciled = reconciled
            self.logger.info(
                "company_source_run_finished sources=%s observations=%s rejected=%s "
                "created=%s duplicates=%s queued=%s suppressed=%s sent=%s errors=%s",
                stats.sources_checked,
                stats.observations,
                stats.rejected,
                stats.canonical_created,
                stats.duplicates,
                stats.deliveries_queued,
                stats.deliveries_suppressed,
                stats.deliveries_sent,
                len(stats.errors),
            )
            return stats
