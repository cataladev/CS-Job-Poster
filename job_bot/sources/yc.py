from __future__ import annotations

from typing import List

from job_bot.models import JobPosting, SourceResult
from job_bot.sources.base import BaseSource, SourceContext
from job_bot.utils.scrape import find_job_links, parse_generic_job_page


class YCombinatorSource(BaseSource):
    async def fetch(self, context: SourceContext) -> SourceResult:
        listing_urls = self.settings.settings.get(
            "listing_urls",
            ["https://www.workatastartup.com/internships"],
        )
        jobs: List[JobPosting] = []
        for listing_url in listing_urls:
            html_text, _, _ = await context.http_client.get_text(listing_url)
            links = find_job_links(html_text, listing_url, r"/jobs/\d+", limit=50)
            for link in links:
                page_html, _, _ = await context.http_client.get_text(link)
                parsed = parse_generic_job_page(page_html)
                if not parsed["title"]:
                    continue
                jobs.append(
                    JobPosting(
                        title=parsed["title"],
                        company=parsed["company"] or "Y Combinator Startup",
                        locations=[parsed["location"] or "Unknown"],
                        source_name=self.name,
                        track=self.track,
                        url=link,
                        canonical_url=link,
                        summary=parsed["summary"],
                        raw_data={"listing_url": listing_url},
                    )
                )
        return SourceResult(self.name, self.track, jobs, metadata={"listing_urls": len(listing_urls)})

