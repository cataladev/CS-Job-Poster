from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Set

from bs4 import BeautifulSoup

from job_bot.models import JobPosting, SourceResult
from job_bot.sources.base import BaseSource, SourceContext
from job_bot.utils.html import collapse_whitespace
from job_bot.watchlist import TrackClassifier, WatchlistService, next_scan_at_for_cadence


class AshbySource(BaseSource):
    async def fetch(self, context: SourceContext) -> SourceResult:
        tokens = await self._board_tokens(context)
        watchlist_targets = await context.database.get_due_company_scan_targets(
            target_type="ashby",
            track=self.track,
            limit=int(self.settings.settings.get("limit", 250)),
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
                    target_type="ashby",
                    jobs_found=len(target_jobs),
                    success=True,
                    next_scan_at=next_scan_at_for_cadence(target["scan_cadence"]),
                )
            except Exception as exc:  # pragma: no cover
                errors.append(f"{target['company']}: {exc}")
                await context.database.mark_watchlist_company_scanned(
                    company_id=int(target["company_id"]),
                    track=self.track,
                    target_type="ashby",
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
        metadata: Dict[str, Any] | None = None,
    ) -> List[JobPosting]:
        board_url = f"https://jobs.ashbyhq.com/{token}"
        html_text, _, _ = await context.http_client.get_text(board_url)
        parsed_jobs = self._parse_board(
            board_url,
            token,
            html_text,
            company_name=company_name,
            metadata=metadata,
        )
        return [job for job in parsed_jobs if classifier.matches(job, self.track)]

    async def _board_tokens(self, context: SourceContext) -> List[str]:
        tokens: Set[str] = set(self.settings.settings.get("board_tokens", []))
        for row in await context.database.get_board_seeds("ashby", self.track):
            tokens.add(row["token"])
        return sorted(tokens)

    def _parse_board(
        self,
        board_url: str,
        token: str,
        html_text: str,
        *,
        company_name: str | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> List[JobPosting]:
        jobs = self._extract_from_next_data(
            board_url,
            token,
            html_text,
            company_name=company_name,
            metadata=metadata,
        )
        if jobs:
            return jobs

        soup = BeautifulSoup(html_text, "html.parser")
        found: List[JobPosting] = []
        seen = set()
        pattern = re.compile(rf"/{re.escape(token)}/[0-9a-fA-F-]+")
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if not pattern.search(href):
                continue
            absolute = f"https://jobs.ashbyhq.com{href}" if href.startswith("/") else href
            if absolute in seen:
                continue
            seen.add(absolute)
            title = collapse_whitespace(anchor.get_text(" ", strip=True))
            parent_text = collapse_whitespace(anchor.parent.get_text(" ", strip=True))
            location = parent_text.replace(title, "").strip(" -|")
            found.append(
                JobPosting(
                    title=title,
                    company=company_name or self._display_name(token),
                    locations=[location or "Unknown"],
                    source_name=self.name,
                    track=self.track,
                    url=absolute,
                    canonical_url=absolute.replace("/application", ""),
                    summary="",
                    raw_data={"board_token": token, **(metadata or {})},
                    provenance_tags=list((metadata or {}).get("watchlist_tags", [])),
                )
            )
        return found

    def _extract_from_next_data(
        self,
        board_url: str,
        token: str,
        html_text: str,
        *,
        company_name: str | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> List[JobPosting]:
        soup = BeautifulSoup(html_text, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        if not script or not script.string:
            return []
        try:
            payload = json.loads(script.string)
        except json.JSONDecodeError:
            return []

        entries = self._walk(payload)
        jobs: List[JobPosting] = []
        seen = set()
        for item in entries:
            title = item.get("title") or item.get("name")
            job_url = item.get("jobUrl") or item.get("url")
            if not title or not job_url:
                continue
            absolute = f"https://jobs.ashbyhq.com{job_url}" if job_url.startswith("/") else job_url
            if absolute in seen:
                continue
            seen.add(absolute)
            location = (
                item.get("locationName")
                or item.get("location")
                or ", ".join(
                    filter(
                        None,
                        [
                            secondary.get("locationName")
                            for secondary in item.get("secondaryLocations", [])
                            if isinstance(secondary, dict)
                        ],
                    )
                )
            )
            jobs.append(
                JobPosting(
                    title=collapse_whitespace(title),
                    company=company_name or self._display_name(token),
                    locations=[location or "Unknown"],
                    source_name=self.name,
                    track=self.track,
                    url=absolute,
                    canonical_url=absolute.replace("/application", ""),
                    source_job_id=item.get("id"),
                    summary="",
                    employment_type=item.get("employmentType"),
                    raw_data={"board_token": token, "raw": item, **(metadata or {})},
                    provenance_tags=list((metadata or {}).get("watchlist_tags", [])),
                )
            )
        return jobs

    def _walk(self, node: Any) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        if isinstance(node, dict):
            if ("title" in node or "name" in node) and ("jobUrl" in node or "url" in node):
                results.append(node)
            for value in node.values():
                results.extend(self._walk(value))
        elif isinstance(node, list):
            for item in node:
                results.extend(self._walk(item))
        return results

    def _display_name(self, token: str) -> str:
        return token.replace("-", " ").replace("_", " ").title()
