from __future__ import annotations

import json
from typing import Any, Dict, List
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from job_bot.models import JobPosting, SourceResult
from job_bot.sources.base import BaseSource, SourceContext
from job_bot.utils.scrape import extract_json_ld_jobs, parse_generic_job_page
from job_bot.watchlist import TrackClassifier, WatchlistService, next_scan_at_for_cadence


class CompanyPagesSource(BaseSource):
    async def fetch(self, context: SourceContext) -> SourceResult:
        watchlist_service = WatchlistService(context.config, context.database)
        classifier = TrackClassifier()
        jobs: List[JobPosting] = []
        errors: List[str] = []

        targets = await context.database.get_due_company_scan_targets(
            target_type="company_page",
            track=self.track,
            limit=int(self.settings.settings.get("limit", 150)),
        )

        for target in targets:
            company_id = int(target["company_id"])
            page_url = json.loads(target["locator"]).get("url", target["careers_url"])
            metadata = watchlist_service.build_watchlist_metadata(target)
            try:
                page_jobs = await self._fetch_page(
                    context=context,
                    company=target["company"],
                    page_url=page_url,
                    metadata=metadata,
                    classifier=classifier,
                )
                for job in page_jobs:
                    if classifier.matches(job, self.track):
                        jobs.append(job)
                await context.database.mark_watchlist_company_scanned(
                    company_id=company_id,
                    track=self.track,
                    target_type="company_page",
                    jobs_found=len(page_jobs),
                    success=True,
                    next_scan_at=next_scan_at_for_cadence(target["scan_cadence"]),
                )
            except Exception as exc:  # pragma: no cover - network/HTML variance
                errors.append(f"{target['company']}: {exc}")
                await context.database.mark_watchlist_company_scanned(
                    company_id=company_id,
                    track=self.track,
                    target_type="company_page",
                    jobs_found=0,
                    success=False,
                    next_scan_at=next_scan_at_for_cadence(target["scan_cadence"]),
                    error=str(exc),
                )

        return SourceResult(
            self.name,
            self.track,
            jobs,
            metadata={"pages": len(targets)},
            errors=errors,
        )

    async def _fetch_page(
        self,
        *,
        context: SourceContext,
        company: str,
        page_url: str,
        metadata: Dict[str, Any],
        classifier: TrackClassifier,
    ) -> List[JobPosting]:
        html_text, _, _ = await context.http_client.get_text(page_url)
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
                elif isinstance(location_payload, dict):
                    address = location_payload.get("address", {})
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
                detail_url = item.get("url") or page_url
                summary = item.get("description", "")
                job = JobPosting(
                    title=title,
                    company=((item.get("hiringOrganization") or {}).get("name") if isinstance(item.get("hiringOrganization"), dict) else None) or company,
                    locations=locations or ["Unknown"],
                    source_name=self.name,
                    track=self.track,
                    url=detail_url,
                    canonical_url=detail_url,
                    summary=summary,
                    posted_date=item.get("datePosted"),
                    raw_data={
                        "page_url": page_url,
                        "raw": item,
                        **metadata,
                    },
                    provenance_tags=list(metadata["watchlist_tags"]),
                )
                if classifier.matches(job, self.track):
                    jobs.append(job)
            return jobs

        soup = BeautifulSoup(html_text, "html.parser")
        for anchor in soup.find_all("a", href=True):
            text = anchor.get_text(" ", strip=True)
            if not text:
                continue
            detail_url = urljoin(page_url, anchor["href"])
            detail_html, _, _ = await context.http_client.get_text(detail_url)
            parsed = parse_generic_job_page(detail_html)
            if not parsed["title"]:
                continue
            job = JobPosting(
                title=parsed["title"],
                company=parsed["company"] or company,
                locations=[parsed["location"] or "Unknown"],
                source_name=self.name,
                track=self.track,
                url=detail_url,
                canonical_url=detail_url,
                summary=parsed["summary"],
                raw_data={"page_url": page_url, **metadata},
                provenance_tags=list(metadata["watchlist_tags"]),
            )
            if classifier.matches(job, self.track):
                jobs.append(job)
        return jobs
