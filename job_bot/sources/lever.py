from __future__ import annotations

from typing import List, Set

from job_bot.models import JobPosting, SourceResult
from job_bot.sources.base import BaseSource, SourceContext
from job_bot.utils.html import strip_html


class LeverSource(BaseSource):
    async def fetch(self, context: SourceContext) -> SourceResult:
        tokens = await self._board_tokens(context)
        jobs: List[JobPosting] = []
        for token in tokens:
            url = f"https://api.lever.co/v0/postings/{token}?mode=json"
            data, _, _ = await context.http_client.get_json(url)
            for item in data:
                categories = item.get("categories", {})
                jobs.append(
                    JobPosting(
                        title=item.get("text", ""),
                        company=self._display_name(token),
                        locations=[categories.get("location", "Unknown")],
                        source_name=self.name,
                        track=self.track,
                        url=item.get("hostedUrl", ""),
                        canonical_url=item.get("hostedUrl", ""),
                        source_job_id=item.get("id"),
                        summary=strip_html(item.get("descriptionPlain") or item.get("description") or ""),
                        posted_date=item.get("createdAt"),
                        employment_type=categories.get("commitment"),
                        raw_data={"board_token": token, "raw": item},
                    )
                )
        return SourceResult(self.name, self.track, jobs, metadata={"boards": len(tokens)})

    async def _board_tokens(self, context: SourceContext) -> List[str]:
        tokens: Set[str] = set(self.settings.settings.get("board_tokens", []))
        for row in await context.database.get_board_seeds("lever", self.track):
            tokens.add(row["token"])
        return sorted(tokens)

    def _display_name(self, token: str) -> str:
        return token.replace("-", " ").replace("_", " ").title()

