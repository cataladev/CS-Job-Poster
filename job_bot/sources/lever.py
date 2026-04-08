from __future__ import annotations

import json
from typing import List, Set

from job_bot.models import JobPosting, SourceResult
from job_bot.sources.base import BaseSource, SourceContext
from job_bot.utils.html import strip_html
from job_bot.watchlist import TrackClassifier, WatchlistService, next_scan_at_for_cadence


class LeverSource(BaseSource):
    async def fetch(self, context: SourceContext) -> SourceResult:
        tokens = await self._board_tokens(context)
        watchlist_targets = await context.database.get_due_company_scan_targets(
            target_type="lever",
            track=self.track,
            limit=int(self.settings.settings.get("limit", 300)),
        )
        classifier = TrackClassifier()
        watchlist_service = WatchlistService(context.config, context.database)
        jobs: List[JobPosting] = []
        errors: List[str] = []
        seen: Set[str] = set()

        for token in tokens:
            seen.add(token)
            try:
                jobs.extend(await self._fetch_board(context, token, classifier))
            except Exception as exc:  # pragma: no cover
                errors.append(f"{token}: {exc}")

        for target in watchlist_targets:
            locator = json.loads(target["locator"])
            token = locator["token"]
            if token in seen:
                continue
            metadata = watchlist_service.build_watchlist_metadata(target)
            try:
                target_jobs = await self._fetch_board(
                    context,
                    token,
                    classifier,
                    company_name=target["company"],
                    metadata=metadata,
                )
                jobs.extend(target_jobs)
                await context.database.mark_watchlist_company_scanned(
                    company_id=int(target["company_id"]),
                    track=self.track,
                    target_type="lever",
                    jobs_found=len(target_jobs),
                    success=True,
                    next_scan_at=next_scan_at_for_cadence(target["scan_cadence"]),
                )
            except Exception as exc:  # pragma: no cover
                errors.append(f"{target['company']}: {exc}")
                await context.database.mark_watchlist_company_scanned(
                    company_id=int(target["company_id"]),
                    track=self.track,
                    target_type="lever",
                    jobs_found=0,
                    success=False,
                    next_scan_at=next_scan_at_for_cadence(target["scan_cadence"]),
                    error=str(exc),
                )

        return SourceResult(
            self.name,
            self.track,
            jobs,
            metadata={"boards": len(tokens) + len(watchlist_targets)},
            errors=errors,
        )

    async def _fetch_board(
        self,
        context: SourceContext,
        token: str,
        classifier: TrackClassifier,
        *,
        company_name: str | None = None,
        metadata: dict | None = None,
    ) -> List[JobPosting]:
        jobs: List[JobPosting] = []
        url = f"https://api.lever.co/v0/postings/{token}?mode=json"
        data, _, _ = await context.http_client.get_json(url)
        for item in data:
            categories = item.get("categories", {})
            job = JobPosting(
                title=item.get("text", ""),
                company=company_name or self._display_name(token),
                locations=[categories.get("location", "Unknown")],
                source_name=self.name,
                track=self.track,
                url=item.get("hostedUrl", ""),
                canonical_url=item.get("hostedUrl", ""),
                source_job_id=item.get("id"),
                summary=strip_html(item.get("descriptionPlain") or item.get("description") or ""),
                posted_date=item.get("createdAt"),
                employment_type=categories.get("commitment"),
                raw_data={"board_token": token, "raw": item, **(metadata or {})},
                provenance_tags=list((metadata or {}).get("watchlist_tags", [])),
            )
            if classifier.matches(job, self.track):
                jobs.append(job)
        return jobs

    async def _board_tokens(self, context: SourceContext) -> List[str]:
        tokens: Set[str] = set(self.settings.settings.get("board_tokens", []))
        for row in await context.database.get_board_seeds("lever", self.track):
            tokens.add(row["token"])
        return sorted(tokens)

    def _display_name(self, token: str) -> str:
        return token.replace("-", " ").replace("_", " ").title()
