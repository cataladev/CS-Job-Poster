from __future__ import annotations

import hashlib
import json
from typing import Dict, List, Optional, Tuple

import markdown
from bs4 import BeautifulSoup, Tag

from job_bot.models import JobPosting, SourceResult
from job_bot.sources.base import BaseSource, SourceContext
from job_bot.utils.html import markdown_link_targets, split_locations, strip_html
from job_bot.utils.normalization import build_row_hash


class GitHubSimplifySource(BaseSource):
    phase = "priority"

    async def fetch(self, context: SourceContext) -> SourceResult:
        repositories = self.settings.settings.get("repositories", [])
        jobs: List[JobPosting] = []
        metadata: Dict[str, object] = {"files_checked": 0, "files_not_modified": 0}

        for repo_config in repositories:
            repository = repo_config["repository"]
            branch = repo_config.get("branch", "main")
            for path in repo_config.get("files", []):
                metadata["files_checked"] = int(metadata["files_checked"]) + 1
                text, headers, status = await self._fetch_file(context, repository, branch, path)
                if status == 304:
                    metadata["files_not_modified"] = int(metadata["files_not_modified"]) + 1
                    continue
                if not text:
                    continue
                file_jobs = self._parse_markdown(
                    repository=repository,
                    path=path,
                    markdown_text=text,
                    track=self.track,
                )
                jobs.extend(file_jobs)

        return SourceResult(
            source_name=self.name,
            track=self.track,
            jobs=jobs,
            metadata=metadata,
        )

    async def _fetch_file(
        self,
        context: SourceContext,
        repository: str,
        branch: str,
        path: str,
    ) -> Tuple[str, Dict[str, str], int]:
        url = f"https://raw.githubusercontent.com/{repository}/{branch}/{path}"
        state = await context.database.get_github_state(repository, path)
        headers = {}
        if state and state["etag"]:
            headers["If-None-Match"] = state["etag"]
        text, response_headers, status = await context.http_client.get_text(url, headers=headers)
        if status != 304:
            content_sha = hashlib.sha1(text.encode("utf-8")).hexdigest()
            await context.database.upsert_github_state(
                repository,
                path,
                etag=response_headers.get("ETag"),
                content_sha=content_sha,
                last_modified=response_headers.get("Last-Modified"),
            )
        return text, response_headers, status

    def _parse_markdown(
        self,
        *,
        repository: str,
        path: str,
        markdown_text: str,
        track: str,
    ) -> List[JobPosting]:
        html = markdown.markdown(markdown_text, extensions=["tables"])
        soup = BeautifulSoup(html, "html.parser")
        jobs: List[JobPosting] = []

        for table in soup.find_all("table"):
            heading = self._nearest_heading(table)
            inactive = self._is_inactive_table(table, path, heading)
            headers = [strip_html(str(cell)) for cell in table.find_all("th")]
            rows = table.find_all("tr")
            last_company = ""

            for row in rows[1:]:
                cells = row.find_all("td")
                if not cells or len(cells) < 4:
                    continue
                data = self._zip_row(headers, cells)
                company = strip_html(data.get("Company", ""))
                if company in {"↳", "->", ""}:
                    company = last_company
                else:
                    last_company = company

                title = data.get("Role", "")
                location_cell = data.get("Location", "")
                locations = split_locations(location_cell)
                application_cell = data.get("Application", "")
                application_urls = markdown_link_targets(application_cell)
                primary_url = self._choose_primary_url(application_urls)
                if not primary_url:
                    primary_url = f"https://github.com/{repository}/blob/main/{path}"

                terms = data.get("Terms", "")
                age = data.get("Age", "")
                summary = ". ".join(
                    part
                    for part in [
                        f"Listed in {heading}" if heading else "",
                        f"Terms: {strip_html(terms)}" if terms else "",
                        f"Age: {strip_html(age)}" if age else "",
                    ]
                    if part
                )

                row_hash = build_row_hash(
                    [
                        repository,
                        path,
                        heading,
                        company,
                        title,
                        "|".join(locations),
                        primary_url,
                        terms,
                    ]
                )

                jobs.append(
                    JobPosting(
                        title=strip_html(title),
                        company=company,
                        locations=locations or ["Unknown"],
                        source_name=self.name,
                        track=track,
                        url=primary_url,
                        canonical_url=primary_url,
                        summary=summary,
                        internship_season=strip_html(terms) or None,
                        season_term=strip_html(terms) or None,
                        raw_data={
                            "repository": repository,
                            "path": path,
                            "heading": heading,
                            "age": strip_html(age),
                            "application_urls": application_urls,
                            "table_headers": headers,
                        },
                        alternate_urls=application_urls,
                        status="inactive" if inactive or "🔒" in application_cell else "active",
                        row_hash=row_hash,
                    )
                )

        return jobs

    def _nearest_heading(self, table: Tag) -> str:
        current: Optional[Tag] = table
        while current is not None:
            sibling = current.previous_sibling
            while sibling is not None:
                if isinstance(sibling, Tag) and sibling.name in {"h1", "h2", "h3", "h4", "h5"}:
                    return strip_html(str(sibling))
                sibling = sibling.previous_sibling
            current = current.parent if isinstance(current.parent, Tag) else None
        return "Unknown section"

    def _is_inactive_table(self, table: Tag, path: str, heading: str) -> bool:
        if "inactive" in path.lower():
            return True
        if "inactive" in heading.lower() or "closed" in heading.lower():
            return True
        details = table.find_parent("details")
        if details:
            summary = details.find("summary")
            if summary and "inactive" in summary.get_text(" ", strip=True).lower():
                return True
        return False

    def _zip_row(self, headers: List[str], cells: List[Tag]) -> Dict[str, str]:
        payload: Dict[str, str] = {}
        for index, header in enumerate(headers):
            if index >= len(cells):
                break
            payload[header] = str(cells[index])
        return payload

    def _choose_primary_url(self, urls: List[str]) -> Optional[str]:
        for url in urls:
            if "simplify.jobs" in url or "imgur.com" in url:
                continue
            return url
        return urls[0] if urls else None
