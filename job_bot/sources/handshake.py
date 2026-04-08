from __future__ import annotations

from typing import List

from job_bot.models import JobPosting, SourceResult
from job_bot.sources.base import BaseSource, SourceContext
from job_bot.utils.scrape import find_job_links, parse_generic_job_page


class HandshakeSource(BaseSource):
    async def fetch(self, context: SourceContext) -> SourceResult:
        if not self.is_enabled(context):
            return SourceResult(self.name, self.track, [], skipped=True)
        listing_urls = self.settings.settings.get("listing_urls", [])
        jobs: List[JobPosting] = []
        for listing_url in listing_urls:
            html_text, _, _ = await context.http_client.get_text(listing_url)
            for link in find_job_links(html_text, listing_url, r"/jobs/\d+", limit=50):
                detail_html, _, _ = await context.http_client.get_text(link)
                parsed = parse_generic_job_page(detail_html)
                if not parsed["title"]:
                    continue
                jobs.append(
                    JobPosting(
                        title=parsed["title"],
                        company=parsed["company"] or "Handshake Company",
                        locations=[parsed["location"] or "Unknown"],
                        source_name=self.name,
                        track=self.track,
                        url=link,
                        canonical_url=link,
                        summary=parsed["summary"],
                    )
                )
        return SourceResult(self.name, self.track, jobs, metadata={"listing_urls": len(listing_urls)})

