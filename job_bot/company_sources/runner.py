from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from job_bot.company_sources.registry import (
    COMPANY_SOURCE_REGISTRY,
    create_company_source,
)
from job_bot.database import Database
from job_bot.feed_pipeline import FeedPipeline, ObservationIngestStats
from job_bot.models import AppConfig
from job_bot.scrape_diagnostics import ScrapeFailure, ScrapeSourceResult, scrape_context
from job_bot.utils.http import AsyncHttpClient

HttpClientFactory = Callable[..., AsyncHttpClient]


def _posted_today(posted_date: str | None, timezone_name: str) -> bool:
    """Return whether a source-provided posting date is today locally.

    Company inventories can contain several days of still-active roles. Only a
    trustworthy same-day date is eligible for delivery; missing or malformed
    dates remain persisted but are suppressed.
    """
    raw = str(posted_date or "").strip()
    if not raw:
        return False
    try:
        posted_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False

    local_timezone = ZoneInfo(timezone_name)
    today = datetime.now(local_timezone).date()
    if posted_at.tzinfo is None:
        return posted_at.date() == today
    return posted_at.astimezone(local_timezone).date() == today


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
    failure_details: list[ScrapeFailure] = field(default_factory=list)

    def add_ingest(self, ingest: ObservationIngestStats) -> None:
        self.observations += ingest.observations
        self.rejected += ingest.rejected
        self.canonical_created += ingest.canonical_created
        self.duplicates += ingest.duplicates
        self.canonicals_merged += ingest.canonicals_merged
        self.deliveries_queued += ingest.deliveries_queued
        self.deliveries_suppressed += ingest.deliveries_suppressed
        self.failure_details.extend(ingest.failure_details)


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
    failure_details: list[ScrapeFailure] = field(default_factory=list)
    source_results: list[ScrapeSourceResult] = field(default_factory=list)

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
        self.failure_details.extend(run.failure_details)


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

        async with lock, scrape_context("initialize source", source=source_key) as context:
            source = create_company_source(source_key, http_client)
            context.source = f"{source_key} ({source.source_id})"
            source_repository = f"company-api/{source_key}"
            source_path = source.source_url
            stats = CompanySourceRunStats(
                source_key=source_key, source_id=source.source_id
            )
            try:
                context.stage = "read feed state"
                state = await self.database.get_feed_state(source.source_id)
                source_baselined = bool(
                    state
                    and state["baseline_complete"]
                    and state["repository"] == source_repository
                    and state["path"] == source_path
                )
                context.stage = "fetch company source"
                result = await source.fetch()
                stats.pages_checked = result.pages_checked
                stats.rejected += result.rejected

                # Process today's rows first so a historical variant of the same
                # canonical cannot create a suppressed delivery before the
                # same-day observation gets a chance to queue it.
                same_day_observations = []
                historical_observations = []
                for observation in result.observations:
                    target = (
                        same_day_observations
                        if _posted_today(
                            observation.posted_date,
                            self.config.schedule_timezone,
                        )
                        else historical_observations
                    )
                    target.append(observation)

                context.stage = "ingest observations"
                ingest_errors: list[str] = []
                for observations, suppress_delivery in (
                    (same_day_observations, False),
                    (historical_observations, True),
                ):
                    if not observations:
                        continue
                    ingest = await self.pipeline.ingest_observations(
                        observations,
                        suppress_delivery=suppress_delivery,
                    )
                    stats.add_ingest(ingest)
                    ingest_errors.extend(ingest.errors)

                if ingest_errors:
                    raise RuntimeError(
                        f"{len(ingest_errors)} observation ingestion error(s); "
                        f"first: {ingest_errors[0]}"
                    )
                context.stage = "record source success"
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
                exc.scrape_stats = stats
                self.logger.exception(
                    "company_source_failed source=%s stage=%s error_type=%s error=%s",
                    source.source_id, context.stage, type(exc).__name__, exc,
                )
                # A closed database must not hide the scraper's original error.
                try:
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
                except Exception as record_error:
                    stats.failure_details.append(ScrapeFailure.from_exception(
                        record_error, source=context.source, stage="record source error"
                    ))
                    self.logger.exception("company_source_error_record_failed source=%s", source.source_id)
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
        stats = CompanySourcePipelineStats(source_results=[
            self._source_result(source_key) for source_key in self.source_keys
        ])
        if self._lock.locked():
            stats.sources_skipped = len(self.source_keys)
            stats.errors.append("company source run already in progress")
            return stats

        async with self._lock, scrape_context("initialize HTTP client", stats=stats) as context:
            semaphore = asyncio.Semaphore(max(1, self.config.source_concurrency))
            async with self.http_client_factory(
                timeout_seconds=self.config.request_timeout_seconds,
                retries=self.config.request_retries,
                user_agent=self.config.user_agent,
            ) as http_client:

                async def run_one(source_key: str) -> CompanySourceRunStats:
                    async with semaphore:
                        return await self.runner.run(source_key, http_client)

                context.stage = "collect company sources"
                results = await asyncio.gather(
                    *(run_one(source_key) for source_key in self.source_keys),
                    return_exceptions=True,
                )
                stats.source_results = [
                    self._source_result(source_key, result)
                    for source_key, result in zip(self.source_keys, results)
                ]

            for source_key, result in zip(self.source_keys, results):
                if isinstance(result, BaseException):
                    failure = ScrapeFailure.from_exception(
                        result, source=source_key, stage="collect company source"
                    )
                    stats.failure_details.append(failure)
                    partial_stats = getattr(result, "scrape_stats", None)
                    if isinstance(partial_stats, CompanySourceRunStats):
                        stats.add_run(partial_stats)
                    else:
                        stats.sources_checked += 1
                        self.logger.error("company_source_failed %s", failure.describe(), exc_info=result)
                    stats.errors.append(f"{source_key}: {result}")
                    continue
                stats.add_run(result)

            context.stage = "publish pending deliveries"
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

    @staticmethod
    def _source_result(
        source_key: str, result: CompanySourceRunStats | BaseException | None = None
    ) -> ScrapeSourceResult:
        source_class = COMPANY_SOURCE_REGISTRY.get(source_key)
        partial = getattr(result, "scrape_stats", None) if isinstance(result, BaseException) else result
        return ScrapeSourceResult(
            source_id=getattr(partial, "source_id", "") or getattr(source_class, "source_id", source_key),
            label=getattr(source_class, "company_name", source_key),
            status=(
                "not_run" if result is None else "failed" if isinstance(result, BaseException)
                else "skipped" if result.skipped else "fetched"
            ),
            observations=getattr(partial, "observations", 0),
            new_jobs=getattr(partial, "canonical_created", 0),
            duplicates=getattr(partial, "duplicates", 0),
            skip_reason="already running",
        )
