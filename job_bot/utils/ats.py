from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Dict, Optional
from urllib.parse import parse_qs, urlparse


GREENHOUSE_JOB_RE = re.compile(r"/jobs?/(\d+)")
LEVER_UUID_RE = re.compile(r"/([0-9a-fA-F-]{32,36})(?:/apply)?/?$")
ASHBY_UUID_RE = re.compile(r"/([0-9a-fA-F-]{32,36})/(?:application|job)?/?$")
ICIMS_JOB_RE = re.compile(r"/jobs?/(\d+)")
SMARTRECRUITERS_RE = re.compile(r"/([0-9]{9,})/?$")
RIPPLING_RE = re.compile(r"/jobs?/([0-9a-fA-F-]{8,36})")


@dataclass
class ExtractedSeed:
    source_name: str
    token: str
    track: str
    discovered_from: str


def extract_external_ids(url: str, source_job_id: Optional[str] = None) -> Dict[str, str]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    ids: Dict[str, str] = {}

    if source_job_id:
        ids["source_job_id"] = source_job_id

    match = GREENHOUSE_JOB_RE.search(parsed.path)
    if match:
        ids["greenhouse_job_id"] = match.group(1)
    if "gh_jid" in query:
        ids["greenhouse_job_id"] = query["gh_jid"][0]

    match = LEVER_UUID_RE.search(parsed.path)
    if match and "lever.co" in parsed.netloc:
        ids["lever_job_id"] = match.group(1)

    match = ASHBY_UUID_RE.search(parsed.path)
    if match and "ashbyhq.com" in parsed.netloc:
        ids["ashby_job_id"] = match.group(1)

    match = ICIMS_JOB_RE.search(parsed.path)
    if match and "icims.com" in parsed.netloc:
        ids["icims_job_id"] = match.group(1)

    match = SMARTRECRUITERS_RE.search(parsed.path)
    if match and "smartrecruiters.com" in parsed.netloc:
        ids["smartrecruiters_job_id"] = match.group(1)

    match = RIPPLING_RE.search(parsed.path)
    if match and "rippling.com" in parsed.netloc:
        ids["rippling_job_id"] = match.group(1)

    workday_fragment = parsed.path.rstrip("/").split("/")[-1]
    if "_" in workday_fragment and "myworkdayjobs.com" in parsed.netloc:
        ids["workday_job_id"] = workday_fragment.split("_")[-1]

    return ids


def extract_board_seed(url: str, track: str) -> Optional[ExtractedSeed]:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]

    if "job-boards.greenhouse.io" in parsed.netloc or "boards.greenhouse.io" in parsed.netloc:
        if parts:
            return ExtractedSeed("greenhouse", parts[0], track, url)

    if "jobs.lever.co" in parsed.netloc and parts:
        return ExtractedSeed("lever", parts[0], track, url)

    if "jobs.ashbyhq.com" in parsed.netloc and parts:
        return ExtractedSeed("ashby", parts[0], track, url)

    if "myworkdayjobs.com" in parsed.netloc and parts:
        tenant = parsed.netloc.split(".")[0]
        site = parts[0]
        token = json.dumps({"host": parsed.netloc, "tenant": tenant, "site": site}, sort_keys=True)
        return ExtractedSeed("workday", token, track, url)

    return None
