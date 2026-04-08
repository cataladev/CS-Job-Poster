from __future__ import annotations

from typing import Dict, List
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from job_bot.models import JobPosting, SourceResult
from job_bot.sources.base import BaseSource, SourceContext
from job_bot.utils.scrape import extract_json_ld_jobs, parse_generic_job_page


class CompanyPagesSource(BaseSource):
    async def fetch(self, context: SourceContext) -> SourceResult:
        pages = self.settings.settings.get("pages", [])
        jobs: List[JobPosting] = []
        for page in pages:
            jobs.extend(await self._fetch_page(context, page))
        return SourceResult(self.name, self.track, jobs, metadata={"pages": len(pages)})

    async def _fetch_page(self, context: SourceContext, page: Dict[str, str]) -> List[JobPosting]:
        url = page["url"]
        company = page.get("company", "Company Careers")
        html_text, _, _ = await context.http_client.get_text(url)
        structured = extract_json_ld_jobs(html_text)
        jobs: List[JobPosting] = []
        if structured:
            for item in structured:
                title = item.get("title") or item.get("name")
                if not title:
                    continue
                locations = []
                location_payload = item.get("jobLocation")
                if isinstance(location_payload, list):
                    for location in location_payload:
                        address = location.get("address", {})
                        if isinstance(address, dict):
                            locations.append(
                                ", ".join(
                                    filter(
                                        None,
                                        [
                                            address.get("addressLocality"),
                                            address.get("addressRegion"),
                                            address.get("addressCountry"),
                                        ],
                                    )
                                )
                            )
                detail_url = item.get("url") or url
                jobs.append(
                    JobPosting(
                        title=title,
                        company=((item.get("hiringOrganization") or {}).get("name") if isinstance(item.get("hiringOrganization"), dict) else None) or company,
                        locations=locations or ["Unknown"],
                        source_name=self.name,
                        track=self.track,
                        url=detail_url,
                        canonical_url=detail_url,
                        summary=item.get("description", ""),
                        raw_data={"page_url": url, "raw": item},
                    )
                )
            return jobs

        soup = BeautifulSoup(html_text, "html.parser")
        for anchor in soup.find_all("a", href=True):
            text = anchor.get_text(" ", strip=True).lower()
            if "intern" not in text and "engineer" not in text:
                continue
            detail_url = urljoin(url, anchor["href"])
            detail_html, _, _ = await context.http_client.get_text(detail_url)
            parsed = parse_generic_job_page(detail_html)
            if not parsed["title"]:
                continue
            jobs.append(
                JobPosting(
                    title=parsed["title"],
                    company=parsed["company"] or company,
                    locations=[parsed["location"] or "Unknown"],
                    source_name=self.name,
                    track=self.track,
                    url=detail_url,
                    canonical_url=detail_url,
                    summary=parsed["summary"],
                    raw_data={"page_url": url},
                )
            )
        return jobs

