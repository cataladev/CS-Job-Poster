from __future__ import annotations

import json
from typing import Dict, List, Set

from job_bot.models import JobPosting, SourceResult
from job_bot.sources.base import BaseSource, SourceContext
from job_bot.utils.html import collapse_whitespace
from job_bot.watchlist import TrackClassifier, WatchlistService, next_scan_at_for_cadence


class WorkdaySource(BaseSource):
    async def fetch(self, context: SourceContext) -> SourceResult:
        boards = await self._boards(context)
        watchlist_targets = await context.database.get_due_company_scan_targets(
            target_type="workday",
            track=self.track,
            limit=int(self.settings.settings.get("limit", 400)),
        )
        classifier = TrackClassifier()
        watchlist_service = WatchlistService(context.config, context.database)
        jobs: List[JobPosting] = []
        errors: List[str] = []
        seen: Set[str] = set()

        for board in boards:
            token = json.dumps(board, sort_keys=True)
            seen.add(token)
            try:
                jobs.extend(await self._fetch_board(context, board, classifier))
            except Exception as exc:  # pragma: no cover
                errors.append(f"{board.get('tenant', 'workday')}: {exc}")

        for target in watchlist_targets:
            locator = json.loads(target["locator"])
            token = locator["token"]
            if token in seen:
                continue
            metadata = watchlist_service.build_watchlist_metadata(target)
            try:
                target_jobs = await self._fetch_board(
                    context,
                    json.loads(token),
                    classifier,
                    company_name=target["company"],
                    metadata=metadata,
                )
                jobs.extend(target_jobs)
                await context.database.mark_watchlist_company_scanned(
                    company_id=int(target["company_id"]),
                    track=self.track,
                    target_type="workday",
                    jobs_found=len(target_jobs),
                    success=True,
                    next_scan_at=next_scan_at_for_cadence(target["scan_cadence"]),
                )
            except Exception as exc:  # pragma: no cover
                errors.append(f"{target['company']}: {exc}")
                await context.database.mark_watchlist_company_scanned(
                    company_id=int(target["company_id"]),
                    track=self.track,
                    target_type="workday",
                    jobs_found=0,
                    success=False,
                    next_scan_at=next_scan_at_for_cadence(target["scan_cadence"]),
                    error=str(exc),
                )

        return SourceResult(
            self.name,
            self.track,
            jobs,
            metadata={"boards": len(boards) + len(watchlist_targets)},
            errors=errors,
        )

    async def _boards(self, context: SourceContext) -> List[Dict[str, str]]:
        boards: List[Dict[str, str]] = []
        seen: Set[str] = set()
        for item in self.settings.settings.get("board_tokens", []):
            payload = item if isinstance(item, dict) else json.loads(item)
            token = json.dumps(payload, sort_keys=True)
            if token in seen:
                continue
            seen.add(token)
            boards.append(payload)
        for row in await context.database.get_board_seeds("workday", self.track):
            payload = json.loads(row["token"])
            token = json.dumps(payload, sort_keys=True)
            if token in seen:
                continue
            seen.add(token)
            boards.append(payload)
        return boards

    async def _fetch_board(
        self,
        context: SourceContext,
        board: Dict[str, str],
        classifier: TrackClassifier,
        *,
        company_name: str | None = None,
        metadata: dict | None = None,
    ) -> List[JobPosting]:
        host = board["host"]
        tenant = board["tenant"]
        site = board["site"]
        company = company_name or board.get("company") or tenant.replace("-", " ").title()
        url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
        offset = 0
        limit = int(self.settings.settings.get("page_size", 20))
        jobs: List[JobPosting] = []
        total = None

        while total is None or offset < total:
            payload = {"limit": limit, "offset": offset, "searchText": ""}
            data, _, _ = await context.http_client.post_json(url, json=payload)
            total = data.get("total")
            for item in data.get("jobPostings", []):
                external_path = item.get("externalPath") or ""
                apply_url = f"https://{host}/{site}{external_path}" if external_path.startswith("/") else external_path
                locations = [collapse_whitespace(location) for location in item.get("locationsText", "").split("|") if collapse_whitespace(location)]
                summary = " | ".join(item.get("bulletFields") or [])
                job = JobPosting(
                    title=item.get("title", ""),
                    company=company,
                    locations=locations or ["Unknown"],
                    source_name=self.name,
                    track=self.track,
                    url=apply_url,
                    canonical_url=apply_url,
                    summary=summary,
                    posted_date=item.get("postedOn"),
                    raw_data={"board": board, "raw": item, **(metadata or {})},
                    provenance_tags=list((metadata or {}).get("watchlist_tags", [])),
                )
                if classifier.matches(job, self.track):
                    jobs.append(job)
            offset += limit
            if not data.get("jobPostings"):
                break
        return jobs
