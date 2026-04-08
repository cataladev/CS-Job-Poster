from __future__ import annotations

import csv
import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from job_bot.database import Database
from job_bot.models import AppConfig, JobPosting
from job_bot.utils.ats import extract_board_seed
from job_bot.utils.normalization import canonicalize_url, normalize_company_name


INTERNSHIP_SIGNALS = {"intern", "internship", "co-op", "coop", "co op", "apprentice"}
NEW_GRAD_SIGNALS = {
    "new grad",
    "new graduate",
    "entry level",
    "early career",
    "graduate",
    "software engineer i",
    "software engineer 1",
    "software developer i",
    "software developer 1",
    "associate software engineer",
}
PRETTY_CATEGORY_NAMES = {
    "requested": "Requested",
    "fortune500": "Fortune500",
    "big_tech": "Big Tech",
    "unicorn": "Unicorn",
    "cloud100": "Cloud100",
    "yc": "YC",
    "startup": "Startup",
}
CADENCE_TO_RUNS = {
    "every_run": 1,
    "2d": 4,
    "7d": 14,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_csv_field(value: str) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_json_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return _parse_csv_field(str(value))
    return [str(item) for item in parsed if str(item).strip()]


def _cadence_next_scan(scan_cadence: str, from_time: Optional[datetime] = None) -> datetime:
    current = from_time or _utc_now()
    if scan_cadence == "2d":
        return current + timedelta(days=2)
    if scan_cadence == "7d":
        return current + timedelta(days=7)
    return current


def next_scan_at_for_cadence(scan_cadence: str) -> str:
    return _cadence_next_scan(scan_cadence).isoformat()


def _initial_next_scan(normalized_company: str, scan_cadence: str) -> datetime:
    current = _utc_now()
    run_slots = CADENCE_TO_RUNS.get(scan_cadence, 1)
    if run_slots <= 1:
        return current
    offset_slot = int(hashlib.sha256(normalized_company.encode("utf-8")).hexdigest(), 16) % run_slots
    return current + timedelta(hours=12 * offset_slot)


def _pretty_provenance_tags(categories: List[str]) -> List[str]:
    tags = [PRETTY_CATEGORY_NAMES.get(category, category.title()) for category in categories]
    return sorted(set(tags))


@dataclass
class TrackClassification:
    track: Optional[str]
    reason: str


class TrackClassifier:
    def classify(
        self,
        *,
        title: str,
        summary: str = "",
        employment_type: str = "",
    ) -> TrackClassification:
        text = " ".join([title.lower(), summary.lower(), employment_type.lower()])
        if self._has_any(text, INTERNSHIP_SIGNALS):
            return TrackClassification("internship", "internship_signal")
        if self._has_any(text, NEW_GRAD_SIGNALS):
            return TrackClassification("new_grad", "new_grad_signal")
        return TrackClassification(None, "no_track_signal")

    def matches(self, job: JobPosting, expected_track: str) -> bool:
        classification = self.classify(
            title=job.title,
            summary=job.summary,
            employment_type=job.employment_type or "",
        )
        return classification.track == expected_track

    def _has_any(self, text: str, phrases: set[str]) -> bool:
        for phrase in phrases:
            if re.search(r"\b" + re.escape(phrase) + r"\b", text):
                return True
        return False


class WatchlistResolver:
    def resolve_targets(self, careers_url: str) -> List[Dict[str, Any]]:
        canonical = canonicalize_url(careers_url)
        targets: List[Dict[str, Any]] = []

        seed = extract_board_seed(canonical, "internship")
        if seed:
            targets.append(
                {
                    "target_type": seed.source_name,
                    "locator": {"token": seed.token, "discovered_from": canonical},
                    "confidence": 0.95,
                }
            )

        if not targets:
            targets.append(
                {
                    "target_type": "company_page",
                    "locator": {"url": canonical},
                    "confidence": 0.75,
                }
            )

        unique: Dict[str, Dict[str, Any]] = {}
        for target in targets:
            key = f"{target['target_type']}::{target['locator']}"
            unique[key] = target
        return list(unique.values())


class WatchlistSeedImporter:
    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config = config
        self.database = database
        self.resolver = WatchlistResolver()

    async def import_seed(self, path: Optional[str] = None) -> Dict[str, int]:
        seed_path = Path(path or self.config.company_seed_path)
        stats = {"rows": 0, "companies_upserted": 0, "targets_upserted": 0}
        if not seed_path.exists():
            return stats

        with seed_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                stats["rows"] += 1
                company = (row.get("company") or "").strip()
                careers_url = canonicalize_url((row.get("careers_url") or "").strip())
                if not company or not careers_url:
                    continue
                normalized_company = normalize_company_name(company)
                categories = _parse_csv_field(row.get("categories", ""))
                tracks = _parse_csv_field(row.get("tracks", "")) or ["both"]
                company_id = await self.database.upsert_watchlist_company(
                    company=company,
                    normalized_company=normalized_company,
                    careers_url=careers_url,
                    homepage_url=(row.get("homepage_url") or "").strip(),
                    country=(row.get("country") or "US").strip(),
                    categories=categories,
                    tier=(row.get("tier") or "t3").strip(),
                    scan_cadence=(row.get("scan_cadence") or "7d").strip(),
                    tracks=tracks,
                    seed_source=(row.get("seed_source") or "seed_snapshot").strip(),
                    notes=(row.get("notes") or "").strip(),
                    active=_parse_bool(row.get("active", "true")),
                    next_scan_at=_initial_next_scan(
                        normalized_company,
                        (row.get("scan_cadence") or "7d").strip(),
                    ).isoformat(),
                )
                stats["companies_upserted"] += 1
                for target in self.resolver.resolve_targets(careers_url):
                    await self.database.upsert_company_scan_target(
                        company_id=company_id,
                        target_type=target["target_type"],
                        locator=target["locator"],
                        confidence=target["confidence"],
                        active=True,
                        verified_at=_utc_now().isoformat(),
                    )
                    stats["targets_upserted"] += 1
        return stats


class WatchlistService:
    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config = config
        self.database = database
        self.importer = WatchlistSeedImporter(config, database)
        self.resolver = self.importer.resolver

    async def sync_seed(self) -> Dict[str, int]:
        return await self.importer.import_seed()

    async def request_company(
        self,
        *,
        company: str,
        careers_url: str,
        requester_user_id: str,
        requester_channel_id: str,
    ) -> int:
        careers_url = canonicalize_url(careers_url.strip())
        if not self._is_valid_public_url(careers_url):
            raise ValueError("A valid public careers URL is required.")
        normalized_company = normalize_company_name(company)
        existing_company = await self.database.find_watchlist_company(company)
        if existing_company and existing_company["careers_url"] == careers_url:
            raise ValueError("That company is already in the watchlist.")
        existing_request = await self.database.find_pending_company_request(
            normalized_company=normalized_company,
            careers_url=careers_url,
        )
        if existing_request:
            return int(existing_request["id"])
        return await self.database.create_company_request(
            company=company.strip(),
            normalized_company=normalized_company,
            careers_url=careers_url,
            requester_user_id=requester_user_id,
            requester_channel_id=requester_channel_id,
        )

    async def approve_request(
        self,
        *,
        request_id: int,
        approver_user_id: str,
    ) -> sqlite3.Row:
        request = await self.database.get_company_request(request_id)
        if not request:
            raise ValueError("Request not found.")
        if request["status"] != "pending":
            raise ValueError("Only pending requests can be approved.")

        company_id = await self.database.upsert_watchlist_company(
            company=request["company"],
            normalized_company=request["normalized_company"],
            careers_url=request["careers_url"],
            homepage_url="",
            country="US",
            categories=["requested"],
            tier="t0",
            scan_cadence="every_run",
            tracks=["both"],
            seed_source="requested",
            notes="Approved from Discord request",
            active=True,
            next_scan_at=_utc_now().isoformat(),
        )
        for target in self.resolver.resolve_targets(request["careers_url"]):
            await self.database.upsert_company_scan_target(
                company_id=company_id,
                target_type=target["target_type"],
                locator=target["locator"],
                confidence=target["confidence"],
                active=True,
                verified_at=_utc_now().isoformat(),
            )
        await self.database.update_company_request(
            request_id=request_id,
            status="approved",
            admin_notes="Approved",
            approver_user_id=approver_user_id,
            watchlist_company_id=company_id,
        )
        company = await self.database.get_watchlist_company_by_id(company_id)
        if not company:
            raise ValueError("Approved company was not found.")
        return company

    async def deny_request(
        self,
        *,
        request_id: int,
        approver_user_id: str,
        reason: str,
    ) -> None:
        request = await self.database.get_company_request(request_id)
        if not request:
            raise ValueError("Request not found.")
        await self.database.update_company_request(
            request_id=request_id,
            status="denied",
            admin_notes=reason or "Denied",
            approver_user_id=approver_user_id,
            watchlist_company_id=None,
        )

    async def list_requests(self, status: Optional[str] = None) -> List[sqlite3.Row]:
        return await self.database.list_company_requests(status=status, limit=25)

    async def list_companies(self, query: str = "", limit: int = 25) -> List[sqlite3.Row]:
        return await self.database.list_watchlist_companies(query=query, active_only=True, limit=limit)

    async def get_company_info(self, query: str) -> Optional[Dict[str, Any]]:
        company = await self.database.find_watchlist_company(query)
        if not company:
            return None
        targets = await self.database.get_company_scan_targets(int(company["id"]))
        return {"company": company, "targets": targets}

    def build_watchlist_metadata(self, company_row: Any) -> Dict[str, Any]:
        categories = _parse_json_list(company_row["categories"])
        return {
            "watchlist_company_id": int(company_row["id"]),
            "watchlist_categories": categories,
            "watchlist_tags": _pretty_provenance_tags(categories),
            "watchlist_tier": company_row["tier"],
            "watchlist_seed_source": company_row["seed_source"],
        }

    def next_scan_at_for_cadence(self, scan_cadence: str) -> str:
        return next_scan_at_for_cadence(scan_cadence)

    def _is_valid_public_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
