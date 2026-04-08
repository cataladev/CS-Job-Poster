from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Set

from job_bot.database import Database
from job_bot.dedupe import DedupeEngine
from job_bot.filters import JobFilter
from job_bot.models import AppConfig, JobPosting, RunStats, SourceResult
from job_bot.publisher import DiscordPublisher
from job_bot.source_manager import SourceManager
from job_bot.sources.base import SourceContext
from job_bot.utils.ats import extract_board_seed
from job_bot.utils.http import AsyncHttpClient
from job_bot.utils.normalization import normalize_job_posting
from job_bot.models import utc_now


class JobPipeline:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        publisher: DiscordPublisher,
    ) -> None:
        self.config = config
        self.database = database
        self.publisher = publisher
        self.job_filter = JobFilter(config)
        self.dedupe = DedupeEngine(database)
        self.logger = logging.getLogger(self.__class__.__name__)
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def run(self, trigger: str = "scheduled") -> RunStats:
        if self._lock.locked():
            stats = RunStats()
            stats.errors.append("run already in progress")
            return stats

        async with self._lock:
            stats = RunStats()
            candidate_post_ids: Set[int] = set()

            async with AsyncHttpClient(
                timeout_seconds=self.config.request_timeout_seconds,
                retries=self.config.request_retries,
                user_agent=self.config.user_agent,
            ) as http_client:
                context = SourceContext(
                    config=self.config,
                    database=self.database,
                    http_client=http_client,
                )
                source_manager = SourceManager(context)

                priority_results = await source_manager.run_priority_sources()
                stats.sources_checked += len(priority_results)
                candidate_post_ids.update(await self._process_results(priority_results, stats))
                await self._publish_candidates(candidate_post_ids, stats)

                async for result in source_manager.iter_secondary_sources():
                    stats.sources_checked += 1
                    new_candidate_ids = await self._process_results([result], stats)
                    if not new_candidate_ids:
                        continue
                    candidate_post_ids.update(new_candidate_ids)
                    await self._publish_candidates(new_candidate_ids, stats)

            stats.finished_at = utc_now()
            return stats

    async def _process_results(self, results: List[SourceResult], stats: RunStats) -> Set[int]:
        candidate_post_ids: Set[int] = set()
        for result in results:
            filtered_count = 0
            new_count = 0
            duplicate_count = 0
            stats.jobs_fetched += len(result.jobs)

            if result.errors:
                stats.errors.extend(result.errors)

            for raw_job in result.jobs:
                job = normalize_job_posting(raw_job)
                await self._discover_seed(job)

                decision = self.job_filter.evaluate(job)
                if not decision.accepted:
                    filtered_count += 1
                    continue

                resolved = await self.dedupe.resolve(job)
                canonical_id, created = await self.database.save_job(
                    job,
                    canonical_id=resolved.canonical_id,
                )

                if resolved.is_duplicate:
                    duplicate_count += 1
                    await self.database.record_dedupe_match(
                        canonical_id=canonical_id,
                        variant_key=job.row_hash or job.canonical_url or job.url,
                        reason=resolved.reason,
                        score=resolved.score,
                    )
                if created:
                    new_count += 1
                if job.status == "active":
                    candidate_post_ids.add(canonical_id)

            stats.jobs_filtered += filtered_count
            stats.jobs_new += new_count
            stats.duplicates_removed += duplicate_count
            await self.database.record_source_run(
                source_name=result.source_name,
                track=result.track,
                status="error" if result.errors else "success",
                jobs_fetched=len(result.jobs),
                jobs_filtered=filtered_count,
                jobs_new=new_count,
                jobs_duplicates=duplicate_count,
                metadata=result.metadata,
                error="; ".join(result.errors) if result.errors else None,
            )
        return candidate_post_ids

    async def _publish_candidates(self, canonical_ids: Set[int], stats: RunStats) -> None:
        pending_ids = set(canonical_ids) - set(stats.posted_canonical_ids)
        if not pending_ids:
            return
        canonical_jobs = await self.database.get_canonical_jobs(sorted(pending_ids))
        posted_ids = await self.publisher.publish_jobs(canonical_jobs)
        if not posted_ids:
            return
        stats.jobs_posted += len(posted_ids)
        stats.posted_canonical_ids.extend(posted_ids)

    async def _discover_seed(self, job: JobPosting) -> None:
        urls = [job.canonical_url or job.url] + list(job.alternate_urls)
        for url in urls:
            seed = extract_board_seed(url, job.track)
            if seed:
                await self.database.upsert_board_seed(
                    seed.source_name,
                    seed.token,
                    seed.track,
                    seed.discovered_from,
                    metadata={"discovered_from_source": job.source_name},
                )
