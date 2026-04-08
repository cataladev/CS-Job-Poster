from __future__ import annotations

import json
from typing import Dict, List, Set

from job_bot.models import JobPosting, SourceResult
from job_bot.sources.base import BaseSource, SourceContext
from job_bot.utils.html import collapse_whitespace


class WorkdaySource(BaseSource):
    async def fetch(self, context: SourceContext) -> SourceResult:
        boards = await self._boards(context)
        jobs: List[JobPosting] = []
        for board in boards:
            jobs.extend(await self._fetch_board(context, board))
        return SourceResult(self.name, self.track, jobs, metadata={"boards": len(boards)})

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

    async def _fetch_board(self, context: SourceContext, board: Dict[str, str]) -> List[JobPosting]:
        host = board["host"]
        tenant = board["tenant"]
        site = board["site"]
        company = board.get("company") or tenant.replace("-", " ").title()
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
                jobs.append(
                    JobPosting(
                        title=item.get("title", ""),
                        company=company,
                        locations=locations or ["Unknown"],
                        source_name=self.name,
                        track=self.track,
                        url=apply_url,
                        canonical_url=apply_url,
                        summary=summary,
                        posted_date=item.get("postedOn"),
                        raw_data={"board": board, "raw": item},
                    )
                )
            offset += limit
            if not data.get("jobPostings"):
                break
        return jobs

