from __future__ import annotations

import asyncio
from typing import List

from job_bot.models import RunStats, SourceResult
from job_bot.sources import build_sources
from job_bot.sources.base import BaseSource, SourceContext


class SourceManager:
    def __init__(self, context: SourceContext) -> None:
        self.context = context
        self.sources = build_sources(context.config)

    def list_enabled_sources(self) -> List[BaseSource]:
        return [source for source in self.sources if source.is_enabled(self.context)]

    async def run_priority_sources(self) -> List[SourceResult]:
        results: List[SourceResult] = []
        for source in self.list_enabled_sources():
            if source.phase != "priority":
                continue
            results.append(await self._safe_fetch(source))
        return results

    async def run_secondary_sources(self) -> List[SourceResult]:
        semaphore = asyncio.Semaphore(self.context.config.source_concurrency)
        tasks = []
        for source in self.list_enabled_sources():
            if source.phase == "priority":
                continue
            tasks.append(self._fetch_with_limit(semaphore, source))
        return await asyncio.gather(*tasks) if tasks else []

    async def _fetch_with_limit(self, semaphore: asyncio.Semaphore, source: BaseSource) -> SourceResult:
        async with semaphore:
            return await self._safe_fetch(source)

    async def _safe_fetch(self, source: BaseSource) -> SourceResult:
        try:
            return await asyncio.wait_for(
                source.fetch(self.context),
                timeout=self.context.config.request_timeout_seconds * 3,
            )
        except Exception as exc:  # pragma: no cover - exercised in integration tests
            source.logger.exception("Source failed: %s", exc)
            return SourceResult(
                source_name=source.name,
                track=source.track,
                jobs=[],
                errors=[str(exc)],
                metadata={"experimental": source.experimental},
            )
