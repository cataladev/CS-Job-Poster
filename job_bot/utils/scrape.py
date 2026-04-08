from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from job_bot.utils.html import collapse_whitespace, strip_html


def find_job_links(html_text: str, base_url: str, href_pattern: str, limit: int = 100) -> List[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    results = []
    seen = set()
    regex = re.compile(href_pattern)
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not regex.search(href):
            continue
        absolute = urljoin(base_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        results.append(absolute)
        if len(results) >= limit:
            break
    return results


def extract_json_ld_jobs(html_text: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html_text, "html.parser")
    jobs: List[Dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(script.string or script.get_text())
        except json.JSONDecodeError:
            continue
        jobs.extend(_flatten_json_ld(payload))
    return jobs


def parse_generic_job_page(html_text: str) -> Dict[str, str]:
    soup = BeautifulSoup(html_text, "html.parser")
    title = ""
    company = ""
    location = ""
    summary = ""

    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        title = collapse_whitespace(og_title["content"])

    if not title and soup.title and soup.title.string:
        title = collapse_whitespace(soup.title.string)

    description = soup.find("meta", attrs={"name": "description"})
    if description and description.get("content"):
        summary = collapse_whitespace(description["content"])

    job_ld = extract_json_ld_jobs(html_text)
    if job_ld:
        first = job_ld[0]
        title = title or collapse_whitespace(first.get("title") or first.get("name") or "")
        company = collapse_whitespace(
            (first.get("hiringOrganization") or {}).get("name")
            if isinstance(first.get("hiringOrganization"), dict)
            else ""
        )
        location = collapse_whitespace(
            _parse_job_location(first.get("jobLocation"))
            or first.get("jobLocationType")
            or ""
        )
        summary = summary or strip_html(first.get("description") or "")

    return {
        "title": title,
        "company": company,
        "location": location,
        "summary": summary,
    }


def _flatten_json_ld(payload: Any) -> List[Dict[str, Any]]:
    jobs: List[Dict[str, Any]] = []
    if isinstance(payload, list):
        for item in payload:
            jobs.extend(_flatten_json_ld(item))
    elif isinstance(payload, dict):
        schema_type = payload.get("@type")
        if schema_type == "JobPosting":
            jobs.append(payload)
        for value in payload.values():
            jobs.extend(_flatten_json_ld(value))
    return jobs


def _parse_job_location(payload: Any) -> str:
    if isinstance(payload, list):
        return " | ".join(filter(None, [_parse_job_location(item) for item in payload]))
    if isinstance(payload, dict):
        address = payload.get("address", {})
        if isinstance(address, dict):
            values = [
                address.get("addressLocality"),
                address.get("addressRegion"),
                address.get("addressCountry"),
            ]
            return ", ".join([value for value in values if value])
    return ""

