from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class JobPosting:
    title: str
    company: str
    locations: List[str]
    source_name: str
    track: str
    url: str
    source_job_id: Optional[str] = None
    canonical_url: Optional[str] = None
    summary: str = ""
    posted_date: Optional[str] = None
    date_found: datetime = field(default_factory=utc_now)
    internship_season: Optional[str] = None
    season_term: Optional[str] = None
    employment_type: Optional[str] = None
    remote_status: str = "unknown"
    source_priority: int = 100
    raw_data: Dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    normalized_company: str = ""
    normalized_title: str = ""
    normalized_locations: List[str] = field(default_factory=list)
    canonical_location: str = ""
    external_ids: Dict[str, str] = field(default_factory=dict)
    alternate_urls: List[str] = field(default_factory=list)
    provenance_tags: List[str] = field(default_factory=list)
    row_hash: Optional[str] = None
    dedupe_fingerprint: Optional[str] = None

    @property
    def location_display(self) -> str:
        if not self.locations:
            return "Unknown"
        return " | ".join(self.locations)


@dataclass
class CanonicalJob:
    id: Optional[int]
    track: str
    title: str
    company: str
    location: str
    canonical_url: str
    summary: str
    season_term: Optional[str]
    employment_type: Optional[str]
    remote_status: str
    status: str
    normalized_company: str
    normalized_title: str
    normalized_locations: List[str]
    dedupe_fingerprint: str
    source_names: List[str]
    provenance_tags: List[str]
    first_seen: datetime
    last_seen: datetime


@dataclass
class WatchlistCompany:
    id: int
    company: str
    normalized_company: str
    careers_url: str
    homepage_url: str
    logo_url: str
    logo_source: str
    logo_verified_at: Optional[datetime]
    country: str
    categories: List[str]
    tier: str
    scan_cadence: str
    tracks: List[str]
    seed_source: str
    notes: str
    active: bool
    last_scan_at: Optional[datetime]
    next_scan_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


@dataclass
class CompanyRequest:
    id: int
    company: str
    normalized_company: str
    careers_url: str
    homepage_url: str
    requester_user_id: str
    requester_channel_id: str
    status: str
    admin_notes: str
    approver_user_id: Optional[str]
    watchlist_company_id: Optional[int]
    created_at: datetime
    updated_at: datetime


@dataclass
class CompanyScanTarget:
    id: int
    company_id: int
    target_type: str
    locator: Dict[str, Any]
    confidence: float
    active: bool
    verified_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


@dataclass
class DedupeDecision:
    is_duplicate: bool
    canonical_id: Optional[int]
    reason: str
    score: Optional[float] = None
    matched_job: Optional[CanonicalJob] = None


@dataclass
class SourceResult:
    source_name: str
    track: str
    jobs: List[JobPosting]
    fetched_at: datetime = field(default_factory=utc_now)
    metadata: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    skipped: bool = False


@dataclass
class RunStats:
    started_at: datetime = field(default_factory=utc_now)
    finished_at: Optional[datetime] = None
    sources_checked: int = 0
    jobs_fetched: int = 0
    jobs_filtered: int = 0
    duplicates_removed: int = 0
    jobs_posted: int = 0
    jobs_new: int = 0
    errors: List[str] = field(default_factory=list)
    posted_canonical_ids: List[int] = field(default_factory=list)


@dataclass
class FeedIngestResult:
    canonical_id: int
    created: bool
    duplicate: bool
    merged_canonical_ids: List[int] = field(default_factory=list)
    delivery_state: Optional[str] = None


@dataclass
class SourceSettings:
    name: str
    source_type: str
    enabled: bool
    priority: int
    track: str
    experimental: bool = False
    settings: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AppConfig:
    discord_token: str
    discord_channel_id: int
    discord_new_grad_channel_id: Optional[int]
    discord_log_channel_id: Optional[int]
    scheduled_run_hours: List[int]
    schedule_timezone: str
    database_url: str
    sqlite_db_path: str
    remote_only: bool
    us_only: bool
    include_coops: bool
    season_filter: List[str]
    company_blacklist: List[str]
    keyword_allowlist: List[str]
    keyword_denylist: List[str]
    log_level: str
    enabled_sources: List[str]
    source_config_path: str
    enable_new_grad_source: bool
    request_timeout_seconds: int
    request_retries: int
    source_concurrency: int
    publish_delay_seconds: float
    max_job_age_hours: int
    user_agent: str
    company_seed_path: str
    source_settings: Dict[str, SourceSettings]
    poll_interval_seconds: int = 300
    recent_catchup_hours: int = 48
    digest_interval_seconds: int = 14_400
    digest_hour_local: int = 16
    digest_timezone: str = "America/New_York"
    direct_ats_poll_interval_seconds: int = 900
    company_source_poll_interval_seconds: int = 3600
    discord_internship_alert_role_id: Optional[int] = None
    discord_new_grad_alert_role_id: Optional[int] = None
    outsource_internship_channel_id: Optional[int] = None
    outsource_new_grad_channel_id: Optional[int] = None

    def channel_id_for_track(self, track: str) -> Optional[int]:
        if track == "new_grad":
            return self.discord_new_grad_channel_id
        return self.discord_channel_id

    def alert_role_id_for_track(self, track: str) -> Optional[int]:
        if track == "new_grad":
            return self.discord_new_grad_alert_role_id
        return self.discord_internship_alert_role_id

    def outsource_channel_id_for_track(self, track: str) -> Optional[int]:
        if track == "new_grad":
            return self.outsource_new_grad_channel_id
        return self.outsource_internship_channel_id

    @property
    def database_dsn(self) -> str:
        return self.database_url or self.sqlite_db_path
