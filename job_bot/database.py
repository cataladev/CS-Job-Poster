from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from job_bot.models import CanonicalJob, FeedIngestResult, JobPosting
from job_bot.utils.feed_normalization import (
    is_aggregator_application_url,
    preferred_application_url,
    role_signature,
    role_title_key,
    sanitize_application_url,
)
from job_bot.utils.normalization import direct_link_company_key, normalize_company_name

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - SQLite tests do not install postgres deps by default.
    psycopg = None
    dict_row = None


DbRow = Mapping[str, Any]
POSTGRES_PRIMARY_RUNTIME_LOCK_ID = 5_942_248_014_240_001_17
RECENT_FEED_CATCHUP_STATE_KEY = "readme_recent_catchup_v1"
SEMANTIC_DEDUPE_WINDOW_DAYS = 3


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True)


def _feed_date(value: str | None) -> datetime.date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _semantic_dedupe_window_matches(
    *,
    incoming_posted_date: str | None,
    candidate_posted_date: str | None,
    candidate_first_seen: str,
) -> bool:
    """Limit metadata-only cross-source matching to the same posting window."""
    incoming = _feed_date(incoming_posted_date) or datetime.now(timezone.utc).date()
    candidate = _feed_date(candidate_posted_date) or _feed_date(candidate_first_seen)
    return candidate is not None and abs((incoming - candidate).days) <= SEMANTIC_DEDUPE_WINDOW_DAYS


def _feed_locations_overlap(
    incoming_locations: set[str], candidate_locations: set[str]
) -> bool:
    """Match a national listing to its specific-city copy, never two city copies.

    Repository rows sometimes only say ``United States`` while the ATS names the
    actual city. That broad U.S. label is safe as a bridge only after company,
    title, track, and cycle have already matched.
    """
    return bool(incoming_locations.intersection(candidate_locations)) or (
        "us" in incoming_locations or "us" in candidate_locations
    )


def _season_terms_compatible(incoming: str, candidate: str) -> bool:
    """Allow a bare year to bridge source-specific season precision.

    A source saying only ``2027`` cannot distinguish a summer role from a winter
    role, so it may match a fully specified 2027 term only when every other
    direct-link guard succeeds. Two explicit, different seasons never match.
    """
    if incoming == candidate or not incoming or not candidate:
        return True
    incoming_year = re.search(r"\b20\d{2}\b", incoming)
    candidate_year = re.search(r"\b20\d{2}\b", candidate)
    return bool(
        incoming_year
        and candidate_year
        and incoming_year.group(0) == candidate_year.group(0)
        and (incoming == incoming_year.group(0) or candidate == candidate_year.group(0))
    )


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT NOT NULL,
    summary TEXT,
    canonical_url TEXT NOT NULL,
    season_term TEXT,
    employment_type TEXT,
    remote_status TEXT,
    status TEXT NOT NULL,
    normalized_company TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    normalized_locations TEXT NOT NULL,
    dedupe_fingerprint TEXT NOT NULL UNIQUE,
    source_names TEXT NOT NULL,
    provenance_tags TEXT NOT NULL DEFAULT '[]',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_id INTEGER NOT NULL REFERENCES canonical_jobs(id) ON DELETE CASCADE,
    track TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_job_id TEXT,
    url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT NOT NULL,
    summary TEXT,
    posted_date TEXT,
    date_found TEXT NOT NULL,
    season_term TEXT,
    employment_type TEXT,
    remote_status TEXT,
    status TEXT NOT NULL,
    normalized_company TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    normalized_locations TEXT NOT NULL,
    external_ids TEXT NOT NULL,
    alternate_urls TEXT NOT NULL,
    provenance_tags TEXT NOT NULL DEFAULT '[]',
    raw_data TEXT NOT NULL,
    row_hash TEXT,
    variant_key TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS fingerprints (
    fingerprint_key TEXT PRIMARY KEY,
    canonical_id INTEGER NOT NULL REFERENCES canonical_jobs(id) ON DELETE CASCADE,
    fingerprint_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discord_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_id INTEGER NOT NULL REFERENCES canonical_jobs(id) ON DELETE CASCADE,
    track TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    UNIQUE(canonical_id, channel_id)
);

CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    track TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL,
    jobs_fetched INTEGER NOT NULL,
    jobs_filtered INTEGER NOT NULL,
    jobs_new INTEGER NOT NULL,
    jobs_duplicates INTEGER NOT NULL,
    metadata TEXT NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS source_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    error TEXT NOT NULL,
    context TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS github_repo_state (
    repository TEXT NOT NULL,
    path TEXT NOT NULL,
    etag TEXT,
    content_sha TEXT,
    last_modified TEXT,
    last_checked_at TEXT NOT NULL,
    PRIMARY KEY(repository, path)
);

CREATE TABLE IF NOT EXISTS board_seeds (
    source_name TEXT NOT NULL,
    token TEXT NOT NULL,
    track TEXT NOT NULL,
    metadata TEXT NOT NULL,
    discovered_from TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(source_name, token, track)
);

CREATE TABLE IF NOT EXISTS dedupe_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_id INTEGER NOT NULL REFERENCES canonical_jobs(id) ON DELETE CASCADE,
    variant_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    score REAL,
    matched_on TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL,
    normalized_company TEXT NOT NULL,
    careers_url TEXT NOT NULL,
    homepage_url TEXT NOT NULL,
    logo_url TEXT NOT NULL DEFAULT '',
    logo_source TEXT NOT NULL DEFAULT '',
    logo_verified_at TEXT,
    country TEXT NOT NULL,
    categories TEXT NOT NULL,
    tier TEXT NOT NULL,
    scan_cadence TEXT NOT NULL,
    tracks TEXT NOT NULL,
    seed_source TEXT NOT NULL,
    notes TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    last_scan_at TEXT,
    next_scan_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(normalized_company, careers_url)
);

CREATE TABLE IF NOT EXISTS company_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL,
    normalized_company TEXT NOT NULL,
    careers_url TEXT NOT NULL,
    homepage_url TEXT NOT NULL DEFAULT '',
    requester_user_id TEXT NOT NULL,
    requester_channel_id TEXT NOT NULL,
    status TEXT NOT NULL,
    admin_notes TEXT NOT NULL,
    approver_user_id TEXT,
    watchlist_company_id INTEGER REFERENCES company_watchlist(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_scan_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES company_watchlist(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL,
    locator TEXT NOT NULL,
    confidence REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    verified_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(company_id, target_type, locator)
);

CREATE TABLE IF NOT EXISTS company_scan_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES company_watchlist(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL,
    track TEXT NOT NULL,
    scanned_at TEXT NOT NULL,
    success INTEGER NOT NULL,
    jobs_found INTEGER NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS job_user_profiles (
    user_id TEXT PRIMARY KEY,
    visibility TEXT NOT NULL DEFAULT 'private',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_application_statuses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES job_user_profiles(user_id) ON DELETE CASCADE,
    company_id INTEGER NOT NULL REFERENCES company_watchlist(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, company_id)
);

CREATE TABLE IF NOT EXISTS process_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES job_user_profiles(user_id) ON DELETE CASCADE,
    company_id INTEGER NOT NULL REFERENCES company_watchlist(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    legacy_status_id INTEGER UNIQUE REFERENCES job_application_statuses(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_process_notes_company_created_at
    ON process_notes(company_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_process_notes_user_company
    ON process_notes(user_id, company_id);

CREATE TABLE IF NOT EXISTS process_role_scopes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER REFERENCES company_watchlist(id) ON DELETE SET NULL,
    canonical_job_id INTEGER REFERENCES canonical_jobs(id) ON DELETE SET NULL,
    legacy_company_id INTEGER UNIQUE REFERENCES company_watchlist(id) ON DELETE SET NULL,
    company TEXT NOT NULL,
    normalized_company TEXT NOT NULL,
    role_title TEXT NOT NULL,
    normalized_role_title TEXT NOT NULL,
    track TEXT NOT NULL,
    season_term TEXT NOT NULL,
    canonical_job_url TEXT NOT NULL DEFAULT '',
    role_key TEXT NOT NULL UNIQUE,
    scope_origin TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS process_role_statuses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES job_user_profiles(user_id) ON DELETE CASCADE,
    scope_id INTEGER NOT NULL REFERENCES process_role_scopes(id) ON DELETE CASCADE,
    legacy_status_id INTEGER UNIQUE REFERENCES job_application_statuses(id) ON DELETE SET NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, scope_id)
);

CREATE TABLE IF NOT EXISTS process_role_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status_id INTEGER NOT NULL REFERENCES process_role_statuses(id) ON DELETE CASCADE,
    scope_id INTEGER NOT NULL REFERENCES process_role_scopes(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES job_user_profiles(user_id) ON DELETE CASCADE,
    legacy_note_id INTEGER UNIQUE REFERENCES process_notes(id) ON DELETE SET NULL,
    body TEXT NOT NULL,
    moderation_status TEXT NOT NULL DEFAULT 'active',
    report_count INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT,
    deleted_by_user_id TEXT,
    deleted_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS process_note_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL REFERENCES process_role_notes(id) ON DELETE CASCADE,
    reporter_user_id TEXT NOT NULL REFERENCES job_user_profiles(user_id) ON DELETE CASCADE,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(note_id, reporter_user_id)
);

CREATE TABLE IF NOT EXISTS process_activity_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES job_user_profiles(user_id) ON DELETE CASCADE,
    scope_id INTEGER REFERENCES process_role_scopes(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_config (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_by_user_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discord_command_receipts (
    message_id TEXT PRIMARY KEY,
    command_name TEXT NOT NULL,
    author_user_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    guild_id TEXT,
    claimed_at TEXT NOT NULL,
    instance_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feed_state (
    source_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    path TEXT NOT NULL,
    etag TEXT,
    content_sha TEXT,
    last_modified TEXT,
    baseline_complete INTEGER NOT NULL DEFAULT 0,
    last_checked_at TEXT NOT NULL,
    last_success_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_target_subscriptions (
    guild_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    track TEXT NOT NULL CHECK (track IN ('internship', 'new_grad')),
    company TEXT NOT NULL,
    company_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id, track, company_key)
);

CREATE TABLE IF NOT EXISTS feed_observations (
    source_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    canonical_id INTEGER NOT NULL REFERENCES canonical_jobs(id) ON DELETE CASCADE,
    source_label TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    posted_date TEXT,
    payload TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY(source_id, external_id)
);

CREATE TABLE IF NOT EXISTS feed_canonical_keys (
    canonical_key TEXT PRIMARY KEY,
    canonical_id INTEGER NOT NULL REFERENCES canonical_jobs(id) ON DELETE CASCADE,
    key_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS direct_link_cache (
    cache_key TEXT PRIMARY KEY,
    track TEXT NOT NULL,
    normalized_company TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    season_term TEXT NOT NULL DEFAULT '',
    normalized_locations TEXT NOT NULL,
    direct_url TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    posted_date TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS direct_ats_cache_state (
    source_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    board_token TEXT NOT NULL,
    last_checked_at TEXT NOT NULL,
    last_success_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS discord_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_id INTEGER NOT NULL REFERENCES canonical_jobs(id) ON DELETE CASCADE,
    channel_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('suppressed', 'pending', 'sending', 'uncertain', 'sent')),
    marker TEXT NOT NULL UNIQUE,
    message_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    not_before TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE(canonical_id, channel_id)
);

CREATE INDEX IF NOT EXISTS idx_process_role_scopes_company
    ON process_role_scopes(normalized_company, track, season_term, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_process_role_statuses_user
    ON process_role_statuses(user_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_process_role_statuses_scope
    ON process_role_statuses(scope_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_process_role_notes_scope_created_at
    ON process_role_notes(scope_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_process_role_notes_status
    ON process_role_notes(status_id, moderation_status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_process_activity_events_user_type
    ON process_activity_events(user_id, event_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_feed_observations_canonical
    ON feed_observations(canonical_id, source_id);

CREATE INDEX IF NOT EXISTS idx_company_target_subscriptions_lookup
    ON company_target_subscriptions(guild_id, track, company_key, created_at);

CREATE INDEX IF NOT EXISTS idx_feed_canonical_match
    ON canonical_jobs(track, normalized_company, season_term);

CREATE INDEX IF NOT EXISTS idx_feed_keys_canonical
    ON feed_canonical_keys(canonical_id);

CREATE INDEX IF NOT EXISTS idx_direct_link_cache_lookup
    ON direct_link_cache(track, normalized_company, normalized_title, season_term, posted_date);

CREATE INDEX IF NOT EXISTS idx_direct_ats_cache_state_checked
    ON direct_ats_cache_state(last_checked_at);

CREATE INDEX IF NOT EXISTS idx_discord_deliveries_state
    ON discord_deliveries(channel_id, state, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_discord_deliveries_canonical
    ON discord_deliveries(canonical_id);
"""

POSTGRES_SCHEMA = SQLITE_SCHEMA.replace(
    "INTEGER PRIMARY KEY AUTOINCREMENT",
    "BIGSERIAL PRIMARY KEY",
)
POSTGRES_SCHEMA = POSTGRES_SCHEMA.replace(
    "INTEGER NOT NULL REFERENCES",
    "BIGINT NOT NULL REFERENCES",
).replace(
    "INTEGER UNIQUE REFERENCES",
    "BIGINT UNIQUE REFERENCES",
).replace(
    "INTEGER REFERENCES",
    "BIGINT REFERENCES",
)


class _SQLiteConnectionAdapter:
    backend = "sqlite"

    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> Any:
        return self._connection.execute(sql, tuple(params))

    def executescript(self, script: str) -> None:
        self._connection.executescript(script)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    def column_names(self, table_name: str) -> set[str]:
        return {
            row["name"]
            for row in self._connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }


class _PostgresConnectionAdapter:
    backend = "postgres"
    _question_mark_re = re.compile(r"\?")

    def __init__(self, dsn: str) -> None:
        if psycopg is None or dict_row is None:
            raise RuntimeError(
                "psycopg is required for PostgreSQL connections. Install the dependencies from requirements.txt."
            )
        self._connection = psycopg.connect(dsn, row_factory=dict_row)

    def execute(self, sql: str, params: Iterable[Any] = ()) -> Any:
        return self._connection.execute(self._translate_sql(sql), tuple(params))

    def executescript(self, script: str) -> None:
        for statement in script.split(";"):
            sql = statement.strip()
            if not sql:
                continue
            self._connection.execute(sql)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    def column_names(self, table_name: str) -> set[str]:
        rows = self.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ?
            """,
            (table_name,),
        ).fetchall()
        return {row["column_name"] for row in rows}

    def _translate_sql(self, sql: str) -> str:
        translated = sql.replace(" COLLATE NOCASE", "")
        if re.match(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\b", translated, flags=re.IGNORECASE):
            translated = re.sub(
                r"INSERT\s+OR\s+IGNORE\s+INTO",
                "INSERT INTO",
                translated,
                count=1,
                flags=re.IGNORECASE,
            ).rstrip()
            translated = f"{translated} ON CONFLICT DO NOTHING"
        return self._question_mark_re.sub("%s", translated)


class Database:
    def __init__(self, dsn_or_path: str) -> None:
        self.dsn_or_path = dsn_or_path
        self.path = self._sqlite_path(dsn_or_path) if not self._is_postgres_dsn(dsn_or_path) else None
        self._lock = asyncio.Lock()
        self._connection = self._build_connection(dsn_or_path)
        self.backend = self._connection.backend
        self._primary_runtime_lock_acquired = False

    async def initialize(self) -> None:
        async with self._lock:
            schema = POSTGRES_SCHEMA if self.backend == "postgres" else SQLITE_SCHEMA
            self._connection.executescript(schema)
            self._ensure_column("canonical_jobs", "provenance_tags", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column("job_variants", "provenance_tags", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column("company_watchlist", "logo_url", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("company_watchlist", "logo_source", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("company_watchlist", "logo_verified_at", "TEXT")
            self._ensure_column("company_requests", "homepage_url", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("job_application_statuses", "notes", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("feed_observations", "source_ref", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("discord_deliveries", "not_before", "TEXT NOT NULL DEFAULT ''")
            # This must run after the additive migration above. Existing databases
            # created before ``not_before`` cannot build this index from the base
            # schema script yet.
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_discord_deliveries_ready
                    ON discord_deliveries(channel_id, state, not_before, created_at)
                """
            )
            self._migrate_legacy_process_notes()
            self._migrate_legacy_process_roles()
            self._seed_legacy_deliveries()
            self._connection.commit()

    async def close(self) -> None:
        async with self._lock:
            self._connection.close()

    async def acquire_primary_runtime_lock(self) -> bool:
        if self.backend != "postgres":
            return True
        async with self._lock:
            if self._primary_runtime_lock_acquired:
                return True
            row = self._connection.execute(
                "SELECT pg_try_advisory_lock(?) AS acquired",
                (POSTGRES_PRIMARY_RUNTIME_LOCK_ID,),
            ).fetchone()
            acquired = bool(row and row["acquired"])
            self._primary_runtime_lock_acquired = acquired
            return acquired

    def _ensure_column(self, table_name: str, column_name: str, column_sql: str) -> None:
        columns = self._connection.column_names(table_name)
        if column_name in columns:
            return
        if self.backend == "postgres":
            self._connection.execute(
                f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {column_sql}"
            )
            return
        self._connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")

    def _build_connection(self, dsn_or_path: str) -> _SQLiteConnectionAdapter | _PostgresConnectionAdapter:
        if self._is_postgres_dsn(dsn_or_path):
            return _PostgresConnectionAdapter(dsn_or_path)
        return _SQLiteConnectionAdapter(self._sqlite_path(dsn_or_path))

    def _insert_and_get_id(self, sql: str, params: Iterable[Any]) -> int:
        if self.backend == "postgres":
            cursor = self._connection.execute(f"{sql.rstrip().rstrip(';')} RETURNING id", params)
            row = cursor.fetchone()
            if not row:
                raise RuntimeError("Insert did not return an id.")
            return int(row["id"])
        cursor = self._connection.execute(sql, params)
        return int(cursor.lastrowid)

    @staticmethod
    def _is_postgres_dsn(dsn_or_path: str) -> bool:
        lowered = dsn_or_path.lower()
        return lowered.startswith("postgres://") or lowered.startswith("postgresql://")

    @staticmethod
    def _sqlite_path(dsn_or_path: str) -> Path:
        if dsn_or_path.lower().startswith("sqlite:///"):
            return Path(dsn_or_path[10:])
        return Path(dsn_or_path)

    def _migrate_legacy_process_notes(self) -> None:
        self._connection.execute(
            """
            INSERT INTO process_notes (
                user_id,
                company_id,
                body,
                legacy_status_id,
                created_at,
                updated_at
            )
            SELECT
                s.user_id,
                s.company_id,
                s.notes,
                s.id,
                COALESCE(NULLIF(s.updated_at, ''), s.created_at),
                COALESCE(NULLIF(s.updated_at, ''), s.created_at)
            FROM job_application_statuses s
            WHERE TRIM(COALESCE(s.notes, '')) <> ''
              AND NOT EXISTS (
                  SELECT 1
                  FROM process_notes pn
                  WHERE pn.legacy_status_id = s.id
              )
            """
        )

    def _migrate_legacy_process_roles(self) -> None:
        # Preserve old company-only process data by materializing one synthetic
        # role scope per company. New writes should target the role-scoped tables
        # directly; this bridge keeps older local databases readable.
        legacy_status_rows = self._connection.execute(
            """
            SELECT
                s.id,
                s.user_id,
                s.company_id,
                s.status,
                s.created_at,
                s.updated_at,
                c.company,
                c.normalized_company
            FROM job_application_statuses s
            JOIN company_watchlist c ON c.id = s.company_id
            """
        ).fetchall()

        if not legacy_status_rows:
            return

        legacy_scope_ids: dict[int, int] = {}

        for row in legacy_status_rows:
            company_id = int(row["company_id"])
            if company_id in legacy_scope_ids:
                continue
            existing_scope = self._connection.execute(
                """
                SELECT id
                FROM process_role_scopes
                WHERE legacy_company_id = ?
                """,
                (company_id,),
            ).fetchone()
            if existing_scope:
                legacy_scope_ids[company_id] = int(existing_scope["id"])
                continue
            scope_id = self._insert_and_get_id(
                """
                INSERT INTO process_role_scopes (
                    company_id,
                    canonical_job_id,
                    legacy_company_id,
                    company,
                    normalized_company,
                    role_title,
                    normalized_role_title,
                    track,
                    season_term,
                    canonical_job_url,
                    role_key,
                    scope_origin,
                    created_at,
                    updated_at
                ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, '', ?, 'legacy', ?, ?)
                """,
                (
                    company_id,
                    company_id,
                    row["company"],
                    row["normalized_company"],
                    "General / Legacy",
                    "general legacy",
                    "legacy",
                    "Legacy",
                    f"legacy:{company_id}",
                    row["created_at"],
                    row["updated_at"],
                ),
            )
            legacy_scope_ids[company_id] = scope_id

        for row in legacy_status_rows:
            company_id = int(row["company_id"])
            scope_id = legacy_scope_ids[company_id]
            existing_status = self._connection.execute(
                """
                SELECT id
                FROM process_role_statuses
                WHERE legacy_status_id = ?
                """,
                (row["id"],),
            ).fetchone()
            if existing_status:
                continue
            self._connection.execute(
                """
                INSERT INTO process_role_statuses (
                    user_id,
                    scope_id,
                    legacy_status_id,
                    status,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    row["user_id"],
                    scope_id,
                    row["id"],
                    row["status"],
                    row["created_at"],
                    row["updated_at"],
                ),
            )

        legacy_note_rows = self._connection.execute(
            """
            SELECT
                n.id,
                n.user_id,
                n.company_id,
                n.body,
                n.created_at,
                n.updated_at,
                s.id AS legacy_status_id
            FROM process_notes n
            JOIN job_application_statuses s
              ON s.user_id = n.user_id
             AND s.company_id = n.company_id
            """
        ).fetchall()

        for row in legacy_note_rows:
            existing_note = self._connection.execute(
                """
                SELECT id
                FROM process_role_notes
                WHERE legacy_note_id = ?
                """,
                (row["id"],),
            ).fetchone()
            if existing_note:
                continue
            status_row = self._connection.execute(
                """
                SELECT id, scope_id
                FROM process_role_statuses
                WHERE legacy_status_id = ?
                """,
                (row["legacy_status_id"],),
            ).fetchone()
            if not status_row:
                continue
            self._connection.execute(
                """
                INSERT INTO process_role_notes (
                    status_id,
                    scope_id,
                    user_id,
                    legacy_note_id,
                    body,
                    moderation_status,
                    report_count,
                    deleted_at,
                    deleted_by_user_id,
                    deleted_reason,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, 'active', 0, NULL, NULL, '', ?, ?)
                """,
                (
                    status_row["id"],
                    status_row["scope_id"],
                    row["user_id"],
                    row["id"],
                    row["body"],
                    row["created_at"],
                    row["updated_at"],
                ),
            )

    def _seed_legacy_deliveries(self) -> None:
        rows = self._connection.execute(
            """
            SELECT canonical_id, channel_id, message_id, posted_at
            FROM discord_posts
            """
        ).fetchall()
        for row in rows:
            canonical_id = int(row["canonical_id"])
            channel_id = str(row["channel_id"])
            marker = self._delivery_marker(canonical_id, channel_id)
            self._connection.execute(
                """
                INSERT OR IGNORE INTO discord_deliveries (
                    canonical_id, channel_id, state, marker, message_id, attempts,
                    last_error, created_at, updated_at, sent_at
                ) VALUES (?, ?, 'sent', ?, ?, 1, NULL, ?, ?, ?)
                """,
                (
                    canonical_id,
                    channel_id,
                    marker,
                    str(row["message_id"]),
                    row["posted_at"],
                    row["posted_at"],
                    row["posted_at"],
                ),
            )

    @staticmethod
    def _delivery_marker(canonical_id: int, channel_id: str) -> str:
        digest = hashlib.sha256(f"{canonical_id}:{channel_id}".encode("utf-8")).hexdigest()[:20]
        return f"jobbot:{digest}"

    def _latest_process_note_join_sql(self) -> str:
        return """
            LEFT JOIN (
                SELECT
                    user_id,
                    company_id,
                    MAX(id) AS latest_note_id,
                    COUNT(*) AS note_count
                FROM process_notes
                GROUP BY user_id, company_id
            ) latest_note_meta
                ON latest_note_meta.user_id = s.user_id
               AND latest_note_meta.company_id = s.company_id
            LEFT JOIN process_notes latest_note
                ON latest_note.id = latest_note_meta.latest_note_id
        """

    def _latest_role_note_join_sql(self) -> str:
        return """
            LEFT JOIN (
                SELECT
                    status_id,
                    MAX(id) AS latest_note_id,
                    COUNT(*) AS note_count
                FROM process_role_notes
                WHERE moderation_status = 'active'
                GROUP BY status_id
            ) latest_role_note_meta
                ON latest_role_note_meta.status_id = s.id
            LEFT JOIN process_role_notes latest_role_note
                ON latest_role_note.id = latest_role_note_meta.latest_note_id
        """

    def _insert_process_note_locked(
        self,
        *,
        user_id: str,
        company_id: int,
        body: str,
        legacy_status_id: Optional[int] = None,
    ) -> DbRow:
        now = _utc_now()
        note_id = self._insert_and_get_id(
            """
            INSERT INTO process_notes (
                user_id,
                company_id,
                body,
                legacy_status_id,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, company_id, body, legacy_status_id, now, now),
        )
        return self._connection.execute(
            "SELECT * FROM process_notes WHERE id = ?",
            (note_id,),
        ).fetchone()

    async def get_feed_state(self, source_id: str) -> Optional[DbRow]:
        async with self._lock:
            return self._connection.execute(
                "SELECT * FROM feed_state WHERE source_id = ?",
                (source_id,),
            ).fetchone()

    async def record_feed_success(
        self,
        *,
        source_id: str,
        repository: str,
        path: str,
        etag: Optional[str],
        content_sha: Optional[str],
        last_modified: Optional[str],
        baseline_complete: bool = True,
    ) -> None:
        async with self._lock:
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO feed_state (
                    source_id, repository, path, etag, content_sha, last_modified,
                    baseline_complete, last_checked_at, last_success_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(source_id) DO UPDATE SET
                    repository = excluded.repository,
                    path = excluded.path,
                    etag = excluded.etag,
                    content_sha = COALESCE(excluded.content_sha, feed_state.content_sha),
                    last_modified = excluded.last_modified,
                    baseline_complete = CASE
                        WHEN feed_state.baseline_complete = 1 THEN 1
                        ELSE excluded.baseline_complete
                    END,
                    last_checked_at = excluded.last_checked_at,
                    last_success_at = excluded.last_success_at,
                    last_error = NULL
                """,
                (
                    source_id,
                    repository,
                    path,
                    etag,
                    content_sha,
                    last_modified,
                    1 if baseline_complete else 0,
                    now,
                    now,
                ),
            )
            self._connection.commit()

    async def record_feed_not_modified(self, source_id: str) -> None:
        async with self._lock:
            now = _utc_now()
            self._connection.execute(
                """
                UPDATE feed_state
                SET last_checked_at = ?, last_success_at = ?, last_error = NULL
                WHERE source_id = ?
                """,
                (now, now, source_id),
            )
            self._connection.commit()

    async def record_feed_error(
        self,
        *,
        source_id: str,
        repository: str,
        path: str,
        error: str,
    ) -> None:
        async with self._lock:
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO feed_state (
                    source_id, repository, path, etag, content_sha, last_modified,
                    baseline_complete, last_checked_at, last_success_at, last_error
                ) VALUES (?, ?, ?, NULL, NULL, NULL, 0, ?, NULL, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    last_checked_at = excluded.last_checked_at,
                    last_error = excluded.last_error
                """,
                (source_id, repository, path, now, error[:2_000]),
            )
            self._connection.commit()

    async def is_initial_feed_baseline_complete(self) -> bool:
        async with self._lock:
            row = self._connection.execute(
                "SELECT value FROM app_state WHERE key = 'initial_feed_baseline_complete'"
            ).fetchone()
            return bool(row and row["value"] == "true")

    async def complete_initial_feed_baseline_if_ready(self, source_ids: Iterable[str]) -> bool:
        values = sorted(set(source_ids))
        if not values:
            return False
        placeholders = ", ".join("?" for _ in values)
        async with self._lock:
            row = self._connection.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM feed_state
                WHERE source_id IN ({placeholders}) AND baseline_complete = 1
                """,
                values,
            ).fetchone()
            if not row or int(row["count"]) != len(values):
                return False
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO app_state (key, value, updated_at)
                VALUES ('initial_feed_baseline_complete', 'true', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (now,),
            )
            self._connection.commit()
            return True

    async def ingest_feed_job(
        self,
        *,
        job: JobPosting,
        source_id: str,
        external_id: str,
        source_label: str,
        source_url: str,
        source_ref: str,
        posted_date: Optional[str],
        payload: Mapping[str, Any],
        canonical_keys: Iterable[str],
        channel_id: Optional[int],
        suppress_delivery: bool,
        delivery_not_before: Optional[str] = None,
    ) -> FeedIngestResult:
        keys = sorted(set(canonical_keys))
        feed_dedupe_fingerprint = role_signature(
            track=job.track,
            company=job.normalized_company,
            title=job.normalized_title,
            cycle=job.season_term,
            normalized_locations=job.normalized_locations,
        )
        # The unique canonical fingerprint represents an individual posting
        # window. Stable URL/requisition matches still merge without this limit.
        feed_dedupe_fingerprint = (
            f"{feed_dedupe_fingerprint}:posted:"
            f"{(_feed_date(posted_date) or datetime.now(timezone.utc).date()).isoformat()}"
        )
        async with self._lock:
            try:
                candidate_ids: set[int] = set()
                observation = self._connection.execute(
                    """
                    SELECT canonical_id FROM feed_observations
                    WHERE source_id = ? AND external_id = ?
                    """,
                    (source_id, external_id),
                ).fetchone()
                if observation:
                    candidate_ids.add(int(observation["canonical_id"]))

                if keys:
                    placeholders = ", ".join("?" for _ in keys)
                    rows = self._connection.execute(
                        f"SELECT DISTINCT canonical_id FROM feed_canonical_keys WHERE canonical_key IN ({placeholders})",
                        keys,
                    ).fetchall()
                    candidate_ids.update(int(row["canonical_id"]) for row in rows)

                if job.canonical_url:
                    rows = self._connection.execute(
                        """
                        SELECT DISTINCT c.id, c.normalized_locations
                        FROM fingerprints f
                        JOIN canonical_jobs c ON c.id = f.canonical_id
                        WHERE f.fingerprint_key = ?
                        """,
                        (f"url:{job.canonical_url}",),
                    ).fetchall()
                    job_locations = set(job.normalized_locations)
                    for row in rows:
                        candidate_locations = set(
                            json.loads(row["normalized_locations"] or "[]")
                        )
                        if _feed_locations_overlap(job_locations, candidate_locations):
                            candidate_ids.add(int(row["id"]))

                job_locations = set(job.normalized_locations)
                rows = self._connection.execute(
                    """
                    SELECT c.id, c.normalized_title, c.normalized_locations,
                           c.season_term, c.first_seen,
                           MIN(NULLIF(o.posted_date, '')) AS first_posted_date
                    FROM canonical_jobs c
                    LEFT JOIN feed_observations o ON o.canonical_id = c.id
                    WHERE c.track = ? AND c.normalized_company = ?
                    GROUP BY c.id
                    """,
                    (job.track, job.normalized_company),
                ).fetchall()
                for row in rows:
                    candidate_locations = set(json.loads(row["normalized_locations"] or "[]"))
                    same_cycle = (row["season_term"] or "").casefold() == (
                        job.season_term or ""
                    ).casefold()
                    same_title = role_title_key(
                        row["normalized_title"], row["season_term"]
                    ) == role_title_key(job.normalized_title, job.season_term)
                    if (
                        same_cycle
                        and same_title
                        and _semantic_dedupe_window_matches(
                            incoming_posted_date=posted_date,
                            candidate_posted_date=row["first_posted_date"],
                            candidate_first_seen=row["first_seen"],
                        )
                        and _feed_locations_overlap(
                            job_locations, candidate_locations
                        )
                    ):
                        candidate_ids.add(int(row["id"]))

                merged_ids: List[int] = []
                if candidate_ids:
                    canonical_id, merged_ids = self._merge_feed_canonicals_locked(candidate_ids)
                    created = False
                else:
                    now = _utc_now()
                    canonical_id = self._insert_and_get_id(
                        """
                        INSERT INTO canonical_jobs (
                            track, title, company, location, summary, canonical_url,
                            season_term, employment_type, remote_status, status,
                            normalized_company, normalized_title, normalized_locations,
                            dedupe_fingerprint, source_names, provenance_tags, first_seen, last_seen
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            job.track,
                            job.title,
                            job.company,
                            job.location_display,
                            job.summary,
                            job.canonical_url or job.url,
                            job.season_term,
                            job.employment_type,
                            job.remote_status,
                            "active",
                            job.normalized_company,
                            job.normalized_title,
                            _json_dump(job.normalized_locations),
                            feed_dedupe_fingerprint,
                            _json_dump([source_id]),
                            _json_dump(job.provenance_tags),
                            now,
                            now,
                        ),
                    )
                    created = True

                canonical_row = self._connection.execute(
                    "SELECT source_names, provenance_tags, summary, canonical_url FROM canonical_jobs WHERE id = ?",
                    (canonical_id,),
                ).fetchone()
                existing_sources = json.loads(canonical_row["source_names"] or "[]")
                existing_provenance = json.loads(canonical_row["provenance_tags"] or "[]")
                merged_sources = sorted(set(existing_sources + [source_id]))
                merged_provenance = sorted(set(existing_provenance + list(job.provenance_tags)))
                canonical_url = preferred_application_url(
                    str(canonical_row["canonical_url"] or ""),
                    job.canonical_url or job.url,
                )
                self._connection.execute(
                    """
                    UPDATE canonical_jobs
                    SET last_seen = ?, source_names = ?, provenance_tags = ?,
                        summary = COALESCE(NULLIF(summary, ''), ?),
                        canonical_url = ?, status = 'active'
                    WHERE id = ?
                    """,
                    (
                        _utc_now(),
                        _json_dump(merged_sources),
                        _json_dump(merged_provenance),
                        job.summary,
                        canonical_url,
                        canonical_id,
                    ),
                )

                variant_key = self._variant_key(job)
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO job_variants (
                        canonical_id, track, source_name, source_job_id, url, canonical_url,
                        title, company, location, summary, posted_date, date_found, season_term,
                        employment_type, remote_status, status, normalized_company,
                        normalized_title, normalized_locations, external_ids, alternate_urls,
                        provenance_tags, raw_data, row_hash, variant_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        canonical_id,
                        job.track,
                        source_id,
                        external_id,
                        job.url,
                        job.canonical_url or job.url,
                        job.title,
                        job.company,
                        job.location_display,
                        job.summary,
                        posted_date,
                        job.date_found.isoformat(),
                        job.season_term,
                        job.employment_type,
                        job.remote_status,
                        "active",
                        job.normalized_company,
                        job.normalized_title,
                        _json_dump(job.normalized_locations),
                        _json_dump(job.external_ids),
                        _json_dump(job.alternate_urls),
                        _json_dump(job.provenance_tags),
                        _json_dump(job.raw_data),
                        job.row_hash,
                        variant_key,
                    ),
                )
                self._store_fingerprints(canonical_id, job)

                now = _utc_now()
                self._connection.execute(
                    """
                    INSERT INTO feed_observations (
                        source_id, external_id, canonical_id, source_label, source_url, source_ref,
                        posted_date, payload, first_seen, last_seen
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id, external_id) DO UPDATE SET
                        canonical_id = excluded.canonical_id,
                        source_label = excluded.source_label,
                        source_url = excluded.source_url,
                        source_ref = excluded.source_ref,
                        posted_date = COALESCE(excluded.posted_date, feed_observations.posted_date),
                        payload = excluded.payload,
                        last_seen = excluded.last_seen
                    """,
                    (
                        source_id,
                        external_id,
                        canonical_id,
                        source_label,
                        source_url,
                        source_ref,
                        posted_date,
                        _json_dump(payload),
                        now,
                        now,
                    ),
                )
                for canonical_key in keys:
                    self._connection.execute(
                        """
                        INSERT OR IGNORE INTO feed_canonical_keys (
                            canonical_key, canonical_id, key_type, created_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (canonical_key, canonical_id, canonical_key.split(":", 1)[0], now),
                    )

                delivery_state: Optional[str] = None
                channel = str(channel_id or 0)
                existing_delivery = self._connection.execute(
                    """
                    SELECT id FROM discord_deliveries
                    WHERE canonical_id = ?
                    """,
                    (canonical_id,),
                ).fetchone()
                if not existing_delivery:
                    delivery_state = "suppressed" if suppress_delivery else "pending"
                    self._connection.execute(
                        """
                        INSERT OR IGNORE INTO discord_deliveries (
                            canonical_id, channel_id, state, marker, message_id, attempts,
                            last_error, created_at, not_before, updated_at, sent_at
                        ) VALUES (?, ?, ?, ?, NULL, 0, NULL, ?, ?, ?, NULL)
                        """,
                        (
                            canonical_id,
                            channel,
                            delivery_state,
                            self._delivery_marker(canonical_id, channel),
                            now,
                            delivery_not_before or "",
                            now,
                        ),
                    )

                self._connection.commit()
                return FeedIngestResult(
                    canonical_id=canonical_id,
                    created=created,
                    duplicate=not created,
                    merged_canonical_ids=merged_ids,
                    delivery_state=delivery_state,
                )
            except Exception:
                self._connection.rollback()
                raise

    async def cache_direct_link(
        self,
        *,
        job: JobPosting,
        source_id: str,
        source_ref: str,
        posted_date: str | None,
    ) -> None:
        """Remember a direct URL already observed by an existing feed.

        This is deliberately a local write only. Feed collectors remain the sole
        source of network work, and each cache record is keyed by a role shape so
        historical URLs never require a table scan during Jobright enrichment.
        """
        direct_url = sanitize_application_url(job.canonical_url or job.url).url
        if not direct_url or is_aggregator_application_url(direct_url):
            return
        season_term = (job.season_term or "").casefold()
        locations = sorted(set(job.normalized_locations))
        cache_payload = {
            "track": job.track,
            "company": direct_link_company_key(job.company),
            "title": job.normalized_title,
            "season": season_term,
            "locations": locations,
            "url": direct_url,
            "source": source_id,
        }
        cache_key = hashlib.sha256(
            json.dumps(cache_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        observed_date = (_feed_date(posted_date) or datetime.now(timezone.utc).date()).isoformat()
        now = _utc_now()
        async with self._lock:
            try:
                self._connection.execute(
                    """
                    INSERT INTO direct_link_cache (
                        cache_key, track, normalized_company, normalized_title, season_term,
                        normalized_locations, direct_url, source_id, source_ref, posted_date,
                        first_seen, last_seen
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        source_ref = excluded.source_ref,
                        posted_date = excluded.posted_date,
                        last_seen = excluded.last_seen
                    """,
                    (
                        cache_key,
                        job.track,
                        direct_link_company_key(job.company),
                        job.normalized_title,
                        season_term,
                        _json_dump(locations),
                        direct_url,
                        source_id,
                        source_ref,
                        observed_date,
                        now,
                        now,
                    ),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    async def find_cached_direct_link(
        self,
        *,
        job: JobPosting,
        posted_date: str | None,
        limit: int = 25,
    ) -> str | None:
        """Return one safe, recent direct URL or ``None`` when a match is ambiguous."""
        season_term = (job.season_term or "").casefold()
        async with self._lock:
            rows = self._connection.execute(
                """
                SELECT direct_url, normalized_locations, season_term, posted_date, first_seen
                FROM direct_link_cache
                WHERE track = ?
                  AND normalized_company = ?
                  AND normalized_title = ?
                ORDER BY last_seen DESC
                LIMIT ?
                """,
                (
                    job.track,
                    direct_link_company_key(job.company),
                    job.normalized_title,
                    max(1, limit),
                ),
            ).fetchall()

        job_locations = set(job.normalized_locations)
        matched_urls: set[str] = set()
        for row in rows:
            direct_url = sanitize_application_url(str(row["direct_url"] or "")).url
            if not direct_url or is_aggregator_application_url(direct_url):
                continue
            candidate_locations = set(json.loads(row["normalized_locations"] or "[]"))
            if not _season_terms_compatible(season_term, str(row["season_term"] or "")):
                continue
            if not _semantic_dedupe_window_matches(
                incoming_posted_date=posted_date,
                candidate_posted_date=row["posted_date"],
                candidate_first_seen=row["first_seen"],
            ):
                continue
            if _feed_locations_overlap(job_locations, candidate_locations):
                matched_urls.add(direct_url)

        return next(iter(matched_urls)) if len(matched_urls) == 1 else None

    async def get_due_direct_ats_sources(
        self,
        source_ids: Iterable[str],
        *,
        stale_before: str,
    ) -> set[str]:
        """Return board sources not checked within the direct-link cache window."""
        values = sorted(set(source_ids))
        if not values:
            return set()
        placeholders = ", ".join("?" for _ in values)
        async with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT source_id
                FROM direct_ats_cache_state
                WHERE source_id IN ({placeholders})
                  AND last_checked_at >= ?
                """,
                (*values, stale_before),
            ).fetchall()
        fresh = {str(row["source_id"]) for row in rows}
        return set(values) - fresh

    async def record_direct_ats_cache_check(
        self,
        *,
        source_id: str,
        provider: str,
        board_token: str,
        error: str | None = None,
    ) -> None:
        """Persist the bounded board-request cooldown, including failures."""
        async with self._lock:
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO direct_ats_cache_state (
                    source_id, provider, board_token, last_checked_at, last_success_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    provider = excluded.provider,
                    board_token = excluded.board_token,
                    last_checked_at = excluded.last_checked_at,
                    last_success_at = excluded.last_success_at,
                    last_error = excluded.last_error
                """,
                (
                    source_id,
                    provider,
                    board_token,
                    now,
                    now if error is None else None,
                    error[:2_000] if error else None,
                ),
            )
            self._connection.commit()

    def _merge_feed_canonicals_locked(self, canonical_ids: Iterable[int]) -> tuple[int, List[int]]:
        values = sorted(set(int(value) for value in canonical_ids))
        if not values:
            raise ValueError("canonical_ids cannot be empty")
        survivor = values[0]
        duplicates = values[1:]
        if not duplicates:
            return survivor, []

        placeholders = ", ".join("?" for _ in values)
        canonical_rows = self._connection.execute(
            f"SELECT id, source_names, provenance_tags, last_seen FROM canonical_jobs WHERE id IN ({placeholders})",
            values,
        ).fetchall()
        source_names: set[str] = set()
        provenance_tags: set[str] = set()
        last_seen = ""
        for row in canonical_rows:
            source_names.update(json.loads(row["source_names"] or "[]"))
            provenance_tags.update(json.loads(row["provenance_tags"] or "[]"))
            last_seen = max(last_seen, str(row["last_seen"] or ""))

        delivery_rank = {"suppressed": 0, "pending": 1, "sending": 2, "uncertain": 3, "sent": 4}
        for duplicate in duplicates:
            duplicate_deliveries = self._connection.execute(
                "SELECT * FROM discord_deliveries WHERE canonical_id = ?",
                (duplicate,),
            ).fetchall()
            for duplicate_delivery in duplicate_deliveries:
                survivor_delivery = self._connection.execute(
                    "SELECT * FROM discord_deliveries WHERE canonical_id = ?",
                    (survivor,),
                ).fetchone()
                if not survivor_delivery:
                    self._connection.execute(
                        "UPDATE discord_deliveries SET canonical_id = ? WHERE id = ?",
                        (survivor, duplicate_delivery["id"]),
                    )
                elif delivery_rank.get(duplicate_delivery["state"], -1) > delivery_rank.get(
                    survivor_delivery["state"], -1
                ):
                    self._connection.execute(
                        "DELETE FROM discord_deliveries WHERE id = ?",
                        (survivor_delivery["id"],),
                    )
                    self._connection.execute(
                        "UPDATE discord_deliveries SET canonical_id = ? WHERE id = ?",
                        (survivor, duplicate_delivery["id"]),
                    )
                else:
                    self._connection.execute(
                        "DELETE FROM discord_deliveries WHERE id = ?",
                        (duplicate_delivery["id"],),
                    )

            duplicate_posts = self._connection.execute(
                "SELECT id, channel_id FROM discord_posts WHERE canonical_id = ?",
                (duplicate,),
            ).fetchall()
            for post in duplicate_posts:
                existing = self._connection.execute(
                    "SELECT id FROM discord_posts WHERE canonical_id = ? AND channel_id = ?",
                    (survivor, post["channel_id"]),
                ).fetchone()
                if existing:
                    self._connection.execute("DELETE FROM discord_posts WHERE id = ?", (post["id"],))
                else:
                    self._connection.execute(
                        "UPDATE discord_posts SET canonical_id = ? WHERE id = ?",
                        (survivor, post["id"]),
                    )

            for table in (
                "job_variants",
                "fingerprints",
                "dedupe_matches",
                "feed_observations",
                "feed_canonical_keys",
            ):
                self._connection.execute(
                    f"UPDATE {table} SET canonical_id = ? WHERE canonical_id = ?",
                    (survivor, duplicate),
                )
            self._connection.execute(
                "UPDATE process_role_scopes SET canonical_job_id = ? WHERE canonical_job_id = ?",
                (survivor, duplicate),
            )
            self._connection.execute("DELETE FROM canonical_jobs WHERE id = ?", (duplicate,))

        self._connection.execute(
            """
            UPDATE canonical_jobs
            SET source_names = ?, provenance_tags = ?, last_seen = ?
            WHERE id = ?
            """,
            (_json_dump(sorted(source_names)), _json_dump(sorted(provenance_tags)), last_seen, survivor),
        )
        return survivor, duplicates

    async def list_deliveries_for_send(self, channel_id: int) -> List[DbRow]:
        now = _utc_now()
        async with self._lock:
            return self._connection.execute(
                """
                SELECT * FROM discord_deliveries
                WHERE channel_id = ? AND state IN ('pending', 'sending', 'uncertain')
                  AND (not_before = '' OR not_before <= ?)
                ORDER BY created_at ASC, id ASC
                """,
                (str(channel_id), now),
            ).fetchall()

    async def list_delivery_channel_ids_for_send(self) -> List[int]:
        now = _utc_now()
        async with self._lock:
            rows = self._connection.execute(
                """
                SELECT DISTINCT channel_id
                FROM discord_deliveries
                WHERE state IN ('pending', 'sending', 'uncertain')
                  AND channel_id <> '0'
                  AND (not_before = '' OR not_before <= ?)
                ORDER BY channel_id ASC
                """,
                (now,),
            ).fetchall()
        return [int(row["channel_id"]) for row in rows]

    @staticmethod
    def _digest_cursor_key(channel_id: int) -> str:
        return f"job_digest_cursor:{channel_id}"

    async def get_digest_cursor(self, channel_id: int) -> Optional[str]:
        async with self._lock:
            row = self._connection.execute(
                "SELECT value FROM app_state WHERE key = ?",
                (self._digest_cursor_key(channel_id),),
            ).fetchone()
        return str(row["value"]) if row else None

    async def set_digest_cursor(self, channel_id: int, cursor: str) -> None:
        async with self._lock:
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO app_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (self._digest_cursor_key(channel_id), cursor, now),
            )
            self._connection.commit()

    async def list_sent_deliveries_since(
        self,
        *,
        channel_id: int,
        sent_after: str,
    ) -> List[DbRow]:
        async with self._lock:
            return self._connection.execute(
                """
                SELECT
                    d.canonical_id,
                    d.message_id,
                    d.sent_at,
                    c.track,
                    c.title,
                    c.company,
                    c.location,
                    c.canonical_url,
                    c.season_term,
                    c.remote_status
                FROM discord_deliveries d
                JOIN canonical_jobs c ON c.id = d.canonical_id
                WHERE d.channel_id = ?
                  AND d.state = 'sent'
                  AND d.sent_at IS NOT NULL
                  AND d.sent_at > ?
                ORDER BY d.sent_at ASC, d.id ASC
                """,
                (str(channel_id), sent_after),
            ).fetchall()

    async def reassign_unsent_deliveries_for_track(
        self,
        *,
        track: str,
        channel_id: int,
    ) -> int:
        """Move safe-to-reroute deliveries without replaying sent/uncertain jobs."""
        async with self._lock:
            try:
                rows = self._connection.execute(
                    """
                    SELECT d.id, d.canonical_id, d.channel_id
                    FROM discord_deliveries d
                    JOIN canonical_jobs c ON c.id = d.canonical_id
                    WHERE c.track = ? AND d.state IN ('suppressed', 'pending')
                    """,
                    (track,),
                ).fetchall()
                moved = 0
                target_channel = str(channel_id)
                for row in rows:
                    if str(row["channel_id"]) == target_channel:
                        continue
                    canonical_id = int(row["canonical_id"])
                    self._connection.execute(
                        """
                        UPDATE discord_deliveries
                        SET channel_id = ?, marker = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            target_channel,
                            self._delivery_marker(canonical_id, target_channel),
                            _utc_now(),
                            row["id"],
                        ),
                    )
                    moved += 1
                self._connection.commit()
                return moved
            except Exception:
                self._connection.rollback()
                raise

    async def recover_misrouted_delivery_channels(
        self,
        *,
        recovery_key: str,
        source_channels: Mapping[str, int],
        target_channels: Mapping[str, int],
        replay_sent_after: str,
    ) -> Dict[str, int]:
        """Restore one accidental cross-server redirect exactly once.

        Unsent deliveries are moved without changing suppression state. Messages
        accepted by Discord after the redirect are requeued with a marker scoped
        to the restored channel, so they can be delivered to the intended server
        without ever duplicating a message in that channel.
        """
        tracks = sorted(set(source_channels).intersection(target_channels))
        if not recovery_key or not tracks:
            return {"channels_restored": 0, "deliveries_moved": 0, "deliveries_requeued": 0}

        async with self._lock:
            try:
                completed = self._connection.execute(
                    "SELECT value FROM app_state WHERE key = ?",
                    (recovery_key,),
                ).fetchone()
                if completed and completed["value"] == "complete":
                    return {
                        "channels_restored": 0,
                        "deliveries_moved": 0,
                        "deliveries_requeued": 0,
                    }

                now = _utc_now()
                channels_restored = 0
                deliveries_moved = 0
                deliveries_requeued = 0

                for track in tracks:
                    source_channel = str(source_channels[track])
                    target_channel = str(target_channels[track])
                    config_key = (
                        "discord_channel_id"
                        if track == "internship"
                        else "discord_new_grad_channel_id"
                    )
                    cursor = self._connection.execute(
                        """
                        UPDATE runtime_config
                        SET value = ?, updated_by_user_id = ?, updated_at = ?
                        WHERE key = ? AND value = ?
                        """,
                        (
                            target_channel,
                            f"system:{recovery_key}",
                            now,
                            config_key,
                            source_channel,
                        ),
                    )
                    channels_restored += max(0, int(cursor.rowcount or 0))

                    rows = self._connection.execute(
                        """
                        SELECT d.id, d.canonical_id, d.state, d.sent_at
                        FROM discord_deliveries d
                        JOIN canonical_jobs c ON c.id = d.canonical_id
                        WHERE c.track = ?
                          AND d.channel_id = ?
                          AND (
                              d.state IN ('suppressed', 'pending', 'sending', 'uncertain')
                              OR (d.state = 'sent' AND d.sent_at >= ?)
                          )
                        ORDER BY d.id ASC
                        """,
                        (track, source_channel, replay_sent_after),
                    ).fetchall()

                    for row in rows:
                        canonical_id = int(row["canonical_id"])
                        target_post = self._connection.execute(
                            """
                            SELECT message_id, posted_at
                            FROM discord_posts
                            WHERE canonical_id = ? AND channel_id = ?
                            """,
                            (canonical_id, target_channel),
                        ).fetchone()
                        if target_post:
                            state = "sent"
                            message_id = str(target_post["message_id"])
                            sent_at = str(target_post["posted_at"])
                            last_error = None
                        elif row["state"] == "suppressed":
                            state = "suppressed"
                            message_id = None
                            sent_at = None
                            last_error = "moved from an unintended Discord server"
                        else:
                            state = "pending"
                            message_id = None
                            sent_at = None
                            last_error = "requeued after an unintended Discord server redirect"
                            if row["state"] in {"sent", "sending", "uncertain"}:
                                deliveries_requeued += 1

                        self._connection.execute(
                            """
                            UPDATE discord_deliveries
                            SET channel_id = ?, state = ?, marker = ?, message_id = ?,
                                last_error = ?, not_before = '', updated_at = ?, sent_at = ?
                            WHERE id = ?
                            """,
                            (
                                target_channel,
                                state,
                                self._delivery_marker(canonical_id, target_channel),
                                message_id,
                                last_error,
                                now,
                                sent_at,
                                row["id"],
                            ),
                        )
                        deliveries_moved += 1

                self._connection.execute(
                    """
                    INSERT INTO app_state (key, value, updated_at)
                    VALUES (?, 'complete', ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (recovery_key, now),
                )
                self._connection.commit()
                return {
                    "channels_restored": channels_restored,
                    "deliveries_moved": deliveries_moved,
                    "deliveries_requeued": deliveries_requeued,
                }
            except Exception:
                self._connection.rollback()
                raise

    async def get_delivery_counts(self) -> Dict[str, int]:
        async with self._lock:
            rows = self._connection.execute(
                "SELECT state, COUNT(*) AS count FROM discord_deliveries GROUP BY state"
            ).fetchall()
        return {str(row["state"]): int(row["count"]) for row in rows}

    async def promote_recent_suppressed_deliveries_once(
        self,
        cutoff_date: str,
        eligible_sources: Iterable[tuple[str, str]],
    ) -> int:
        """Queue recent baseline observations exactly once after an existing deployment upgrades."""
        source_pairs = sorted(set(eligible_sources))
        async with self._lock:
            try:
                state = self._connection.execute(
                    "SELECT value FROM app_state WHERE key = ?",
                    (RECENT_FEED_CATCHUP_STATE_KEY,),
                ).fetchone()
                if state and state["value"] == "complete":
                    return 0

                now = _utc_now()
                source_predicate = " OR ".join(
                    "(o.source_id = ? AND o.source_ref = ?)" for _ in source_pairs
                ) or "0 = 1"
                source_params = [value for pair in source_pairs for value in pair]
                cursor = self._connection.execute(
                    f"""
                    UPDATE discord_deliveries
                    SET state = 'pending', updated_at = ?
                    WHERE state = 'suppressed'
                      AND EXISTS (
                          SELECT 1
                          FROM feed_observations o
                          WHERE o.canonical_id = discord_deliveries.canonical_id
                            AND o.posted_date IS NOT NULL
                            AND o.posted_date >= ?
                            AND ({source_predicate})
                      )
                    """,
                    (now, cutoff_date, *source_params),
                )
                promoted = max(0, int(cursor.rowcount or 0))
                self._connection.execute(
                    """
                    INSERT INTO app_state (key, value, updated_at)
                    VALUES (?, 'complete', ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (RECENT_FEED_CATCHUP_STATE_KEY, now),
                )
                self._connection.commit()
                return promoted
            except Exception:
                self._connection.rollback()
                raise

    async def mark_recent_suppressed_catchup_complete(self) -> None:
        """Prevent a fresh installation from replaying its intentionally silent baseline."""
        async with self._lock:
            try:
                now = _utc_now()
                self._connection.execute(
                    """
                    INSERT INTO app_state (key, value, updated_at)
                    VALUES (?, 'complete', ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (RECENT_FEED_CATCHUP_STATE_KEY, now),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    async def mark_delivery_sending(self, delivery_id: int) -> None:
        async with self._lock:
            try:
                self._connection.execute(
                    """
                    UPDATE discord_deliveries
                    SET state = 'sending', attempts = attempts + 1, last_error = NULL, updated_at = ?
                    WHERE id = ? AND state IN ('pending', 'sending', 'uncertain')
                    """,
                    (_utc_now(), delivery_id),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    async def mark_delivery_uncertain(self, delivery_id: int, error: str) -> None:
        async with self._lock:
            try:
                self._connection.execute(
                    """
                    UPDATE discord_deliveries
                    SET state = 'uncertain', last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (error[:2_000], _utc_now(), delivery_id),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    async def mark_delivery_suppressed(self, delivery_id: int, reason: str) -> None:
        async with self._lock:
            try:
                self._connection.execute(
                    """
                    UPDATE discord_deliveries
                    SET state = 'suppressed', last_error = ?, updated_at = ?
                    WHERE id = ? AND state IN ('pending', 'sending', 'uncertain')
                    """,
                    (reason[:2_000], _utc_now(), delivery_id),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    async def mark_delivery_sent(
        self,
        *,
        delivery_id: int,
        canonical_id: int,
        track: str,
        channel_id: int,
        message_id: int,
    ) -> None:
        async with self._lock:
            try:
                now = _utc_now()
                self._connection.execute(
                    """
                    UPDATE discord_deliveries
                    SET state = 'sent', message_id = ?, last_error = NULL, updated_at = ?, sent_at = ?
                    WHERE id = ?
                    """,
                    (str(message_id), now, now, delivery_id),
                )
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO discord_posts (
                        canonical_id, track, channel_id, message_id, posted_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (canonical_id, track, str(channel_id), str(message_id), now),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    async def get_feed_observation_metadata(self, canonical_id: int) -> Dict[str, Any]:
        async with self._lock:
            rows = self._connection.execute(
                """
                SELECT source_id, source_label, source_url, source_ref, posted_date, payload
                FROM feed_observations
                WHERE canonical_id = ?
                ORDER BY source_label ASC, source_id ASC
                """,
                (canonical_id,),
            ).fetchall()
        labels = []
        seen = set()
        dates = []
        company_logo_urls = []
        jobright_fallback_urls = []
        for row in rows:
            if row["source_label"] not in seen:
                seen.add(row["source_label"])
                labels.append(row["source_label"])
            if row["posted_date"]:
                dates.append(row["posted_date"])
            try:
                payload = json.loads(str(row["payload"] or "{}"))
            except (TypeError, json.JSONDecodeError):
                payload = {}
            raw_data = payload.get("raw_data") if isinstance(payload, Mapping) else None
            logo_url = raw_data.get("company_logo_url") if isinstance(raw_data, Mapping) else None
            if isinstance(logo_url, str) and logo_url and logo_url not in company_logo_urls:
                company_logo_urls.append(logo_url)
            fallback_url = (
                raw_data.get("jobright_fallback_url") if isinstance(raw_data, Mapping) else None
            )
            safe_fallback_url = sanitize_application_url(str(fallback_url or "")).url
            if safe_fallback_url and is_aggregator_application_url(safe_fallback_url):
                jobright_fallback_urls.append(safe_fallback_url)
        return {
            "source_labels": labels,
            "sources": [
                {
                    "label": row["source_label"],
                    "url": row["source_url"],
                    "ref": row["source_ref"],
                }
                for row in rows
                if row["source_label"] and row["source_url"]
            ],
            "posted_date": min(dates) if dates else None,
            "company_logo_url": company_logo_urls[0] if company_logo_urls else None,
            "jobright_fallback_url": (
                sorted(set(jobright_fallback_urls))[0] if jobright_fallback_urls else None
            ),
        }

    async def get_runtime_config_value(self, key: str) -> Optional[DbRow]:
        async with self._lock:
            return self._connection.execute(
                """
                SELECT key, value, updated_by_user_id, updated_at
                FROM runtime_config
                WHERE key = ?
                """,
                (key,),
            ).fetchone()

    async def set_runtime_config_value(
        self,
        key: str,
        value: Optional[str],
        updated_by_user_id: str,
    ) -> DbRow:
        async with self._lock:
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO runtime_config (key, value, updated_by_user_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_by_user_id = excluded.updated_by_user_id,
                    updated_at = excluded.updated_at
                """,
                (key, value, updated_by_user_id, now),
            )
            self._connection.commit()
            return self._connection.execute(
                """
                SELECT key, value, updated_by_user_id, updated_at
                FROM runtime_config
                WHERE key = ?
                """,
                (key,),
            ).fetchone()

    async def list_runtime_config(self) -> List[DbRow]:
        async with self._lock:
            return self._connection.execute(
                """
                SELECT key, value, updated_by_user_id, updated_at
                FROM runtime_config
                ORDER BY key ASC
                """
            ).fetchall()

    async def toggle_company_target_subscription(
        self,
        *,
        guild_id: int,
        user_id: int,
        track: str,
        company: str,
        company_key: str,
        max_targets_per_user: int = 50,
        max_subscribers_per_target: int = 50,
    ) -> bool:
        """Toggle a target subscription and return whether it is enabled afterward."""
        if track not in {"internship", "new_grad"}:
            raise ValueError("Track must be internship or new_grad.")
        if guild_id <= 0 or user_id <= 0:
            raise ValueError("Guild and user IDs must be positive.")
        if not company.strip() or not company_key.strip():
            raise ValueError("Company must not be empty.")

        async with self._lock:
            try:
                key = (str(guild_id), str(user_id), track, company_key)
                existing = self._connection.execute(
                    """
                    SELECT 1
                    FROM company_target_subscriptions
                    WHERE guild_id = ? AND user_id = ? AND track = ? AND company_key = ?
                    """,
                    key,
                ).fetchone()
                if existing:
                    self._connection.execute(
                        """
                        DELETE FROM company_target_subscriptions
                        WHERE guild_id = ? AND user_id = ? AND track = ? AND company_key = ?
                        """,
                        key,
                    )
                    self._connection.commit()
                    return False

                user_count = self._connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM company_target_subscriptions
                    WHERE guild_id = ? AND user_id = ?
                    """,
                    (str(guild_id), str(user_id)),
                ).fetchone()
                if user_count and int(user_count["count"]) >= max_targets_per_user:
                    raise ValueError(
                        f"You can target at most {max_targets_per_user} company/track combinations."
                    )

                subscriber_count = self._connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM company_target_subscriptions
                    WHERE guild_id = ? AND track = ? AND company_key = ?
                    """,
                    (str(guild_id), track, company_key),
                ).fetchone()
                if (
                    subscriber_count
                    and int(subscriber_count["count"]) >= max_subscribers_per_target
                ):
                    raise ValueError(
                        "That company and track already has the maximum number of subscribers."
                    )

                self._connection.execute(
                    """
                    INSERT INTO company_target_subscriptions (
                        guild_id, user_id, track, company, company_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(guild_id),
                        str(user_id),
                        track,
                        company,
                        company_key,
                        _utc_now(),
                    ),
                )
                self._connection.commit()
                return True
            except Exception:
                self._connection.rollback()
                raise

    async def list_company_target_subscriber_ids(
        self,
        *,
        guild_id: int,
        track: str,
        company_key: str,
    ) -> List[int]:
        async with self._lock:
            rows = self._connection.execute(
                """
                SELECT user_id
                FROM company_target_subscriptions
                WHERE guild_id = ? AND track = ? AND company_key = ?
                ORDER BY created_at ASC, user_id ASC
                """,
                (str(guild_id), track, company_key),
            ).fetchall()
        return [int(row["user_id"]) for row in rows]

    async def claim_command_receipt(
        self,
        *,
        message_id: str,
        command_name: str,
        author_user_id: str,
        channel_id: str,
        guild_id: Optional[str],
        instance_id: str,
    ) -> bool:
        async with self._lock:
            claimed_at = _utc_now()
            cursor = self._connection.execute(
                """
                INSERT INTO discord_command_receipts (
                    message_id,
                    command_name,
                    author_user_id,
                    channel_id,
                    guild_id,
                    claimed_at,
                    instance_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO NOTHING
                """,
                (
                    message_id,
                    command_name,
                    author_user_id,
                    channel_id,
                    guild_id,
                    claimed_at,
                    instance_id,
                ),
            )
            self._connection.commit()
            return bool(getattr(cursor, "rowcount", 0))

    async def get_github_state(self, repository: str, path: str) -> Optional[DbRow]:
        async with self._lock:
            cursor = self._connection.execute(
                "SELECT * FROM github_repo_state WHERE repository = ? AND path = ?",
                (repository, path),
            )
            return cursor.fetchone()

    async def upsert_github_state(
        self,
        repository: str,
        path: str,
        *,
        etag: Optional[str],
        content_sha: Optional[str],
        last_modified: Optional[str],
    ) -> None:
        async with self._lock:
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO github_repo_state (repository, path, etag, content_sha, last_modified, last_checked_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(repository, path) DO UPDATE SET
                    etag = excluded.etag,
                    content_sha = excluded.content_sha,
                    last_modified = excluded.last_modified,
                    last_checked_at = excluded.last_checked_at
                """,
                (repository, path, etag, content_sha, last_modified, now),
            )
            self._connection.commit()

    async def upsert_board_seed(
        self,
        source_name: str,
        token: str,
        track: str,
        discovered_from: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        async with self._lock:
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO board_seeds (source_name, token, track, metadata, discovered_from, first_seen, last_seen, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(source_name, token, track) DO UPDATE SET
                    metadata = excluded.metadata,
                    discovered_from = excluded.discovered_from,
                    last_seen = excluded.last_seen,
                    active = 1
                """,
                (
                    source_name,
                    token,
                    track,
                    _json_dump(metadata or {}),
                    discovered_from,
                    now,
                    now,
                ),
            )
            self._connection.commit()

    async def get_board_seeds(self, source_name: str, track: Optional[str] = None) -> List[DbRow]:
        query = "SELECT * FROM board_seeds WHERE source_name = ? AND active = 1"
        params: List[Any] = [source_name]
        if track:
            query += " AND track = ?"
            params.append(track)
        async with self._lock:
            cursor = self._connection.execute(query, params)
            return cursor.fetchall()

    async def upsert_watchlist_company(
        self,
        *,
        company: str,
        normalized_company: str,
        careers_url: str,
        homepage_url: str,
        country: str,
        categories: List[str],
        tier: str,
        scan_cadence: str,
        tracks: List[str],
        seed_source: str,
        notes: str,
        active: bool,
        next_scan_at: Optional[str],
        logo_url: str = "",
        logo_source: str = "",
        logo_verified_at: Optional[str] = None,
    ) -> int:
        async with self._lock:
            now = _utc_now()
            existing = self._connection.execute(
                """
                SELECT id, categories, tracks, notes, homepage_url, logo_url, logo_source, logo_verified_at
                FROM company_watchlist
                WHERE normalized_company = ? AND careers_url = ?
                """,
                (normalized_company, careers_url),
            ).fetchone()
            if existing:
                merged_categories = sorted(
                    set(json.loads(existing["categories"] or "[]") + categories)
                )
                merged_tracks = sorted(set(json.loads(existing["tracks"] or "[]") + tracks))
                merged_notes = " | ".join(
                    part for part in [existing["notes"], notes] if part
                )[:1000]
                resolved_homepage = homepage_url or existing["homepage_url"] or ""
                resolved_logo_url = logo_url or existing["logo_url"] or ""
                resolved_logo_source = logo_source or existing["logo_source"] or ""
                resolved_logo_verified_at = logo_verified_at or existing["logo_verified_at"]
                self._connection.execute(
                    """
                    UPDATE company_watchlist
                    SET company = ?,
                        homepage_url = ?,
                        logo_url = ?,
                        logo_source = ?,
                        logo_verified_at = ?,
                        country = ?,
                        categories = ?,
                        tier = ?,
                        scan_cadence = ?,
                        tracks = ?,
                        seed_source = ?,
                        notes = ?,
                        active = ?,
                        next_scan_at = COALESCE(next_scan_at, ?),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        company,
                        resolved_homepage,
                        resolved_logo_url,
                        resolved_logo_source,
                        resolved_logo_verified_at,
                        country,
                        _json_dump(merged_categories),
                        tier,
                        scan_cadence,
                        _json_dump(merged_tracks),
                        seed_source,
                        merged_notes,
                        1 if active else 0,
                        next_scan_at,
                        now,
                        existing["id"],
                    ),
                )
                self._connection.commit()
                return int(existing["id"])

            company_id = self._insert_and_get_id(
                """
                INSERT INTO company_watchlist (
                    company, normalized_company, careers_url, homepage_url,
                    logo_url, logo_source, logo_verified_at, country,
                    categories, tier, scan_cadence, tracks, seed_source, notes,
                    active, next_scan_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    company,
                    normalized_company,
                    careers_url,
                    homepage_url,
                    logo_url,
                    logo_source,
                    logo_verified_at,
                    country,
                    _json_dump(categories),
                    tier,
                    scan_cadence,
                    _json_dump(tracks),
                    seed_source,
                    notes,
                    1 if active else 0,
                    next_scan_at,
                    now,
                    now,
                ),
            )
            self._connection.commit()
            return company_id

    async def list_watchlist_companies(
        self,
        *,
        query: str = "",
        active_only: bool = True,
        limit: int = 50,
    ) -> List[DbRow]:
        sql = "SELECT * FROM company_watchlist WHERE 1 = 1"
        params: List[Any] = []
        if active_only:
            sql += " AND active = 1"
        if query:
            like = f"%{query.lower()}%"
            sql += " AND (lower(company) LIKE ? OR lower(tier) LIKE ? OR lower(categories) LIKE ?)"
            params.extend([like, like, like])
        sql += " ORDER BY lower(company) ASC, tier ASC LIMIT ?"
        params.append(limit)
        async with self._lock:
            return self._connection.execute(sql, params).fetchall()

    async def get_watchlist_company_by_id(self, company_id: int) -> Optional[DbRow]:
        async with self._lock:
            return self._connection.execute(
                "SELECT * FROM company_watchlist WHERE id = ?",
                (company_id,),
            ).fetchone()

    async def find_watchlist_company(self, query: str) -> Optional[DbRow]:
        async with self._lock:
            if query.isdigit():
                row = self._connection.execute(
                    "SELECT * FROM company_watchlist WHERE id = ?",
                    (int(query),),
                ).fetchone()
                if row:
                    return row
            normalized = normalize_company_name(query)
            return self._connection.execute(
                """
                SELECT *
                FROM company_watchlist
                WHERE normalized_company = ? OR lower(company) = ?
                ORDER BY active DESC, updated_at DESC
                LIMIT 1
                """,
                (normalized, normalized),
            ).fetchone()

    async def update_watchlist_company_name(self, company_id: int, company: str) -> bool:
        async with self._lock:
            existing = self._connection.execute(
                "SELECT company FROM company_watchlist WHERE id = ?",
                (company_id,),
            ).fetchone()
            if not existing or existing["company"] == company:
                return False
            self._connection.execute(
                """
                UPDATE company_watchlist
                SET company = ?, updated_at = ?
                WHERE id = ?
                """,
                (company, _utc_now(), company_id),
            )
            self._connection.commit()
            return True

    async def get_hot_watchlist_company_names(self) -> Dict[str, str]:
        async with self._lock:
            rows = self._connection.execute(
                """
                SELECT normalized_company, company
                FROM company_watchlist
                WHERE active = 1 AND company LIKE '🔥%'
                ORDER BY updated_at DESC
                """
            ).fetchall()
        hot_names: Dict[str, str] = {}
        for row in rows:
            hot_names.setdefault(row["normalized_company"], row["company"])
        return hot_names

    async def get_watchlist_company_by_normalized_company(
        self,
        normalized_company: str,
    ) -> Optional[DbRow]:
        async with self._lock:
            return self._connection.execute(
                """
                SELECT *
                FROM company_watchlist
                WHERE normalized_company = ?
                ORDER BY active DESC, updated_at DESC
                LIMIT 1
                """,
                (normalized_company,),
            ).fetchone()

    async def get_company_branding(self, normalized_company: str) -> Optional[DbRow]:
        async with self._lock:
            return self._connection.execute(
                """
                SELECT company, normalized_company, homepage_url, careers_url,
                       logo_url, logo_source, logo_verified_at
                FROM company_watchlist
                WHERE normalized_company = ?
                ORDER BY
                    CASE WHEN logo_url <> '' THEN 0 ELSE 1 END,
                    CASE WHEN homepage_url <> '' THEN 0 ELSE 1 END,
                    active DESC,
                    updated_at DESC
                LIMIT 1
                """,
                (normalized_company,),
            ).fetchone()

    async def update_watchlist_branding(
        self,
        *,
        company_id: int,
        homepage_url: Optional[str] = None,
        logo_url: Optional[str] = None,
        logo_source: Optional[str] = None,
        logo_verified_at: Optional[str] = None,
    ) -> bool:
        async with self._lock:
            existing = self._connection.execute(
                """
                SELECT homepage_url, logo_url, logo_source, logo_verified_at
                FROM company_watchlist
                WHERE id = ?
                """,
                (company_id,),
            ).fetchone()
            if not existing:
                return False

            resolved_homepage = existing["homepage_url"] if homepage_url is None else homepage_url
            resolved_logo_url = existing["logo_url"] if logo_url is None else logo_url
            resolved_logo_source = existing["logo_source"] if logo_source is None else logo_source
            resolved_logo_verified_at = (
                existing["logo_verified_at"] if logo_verified_at is None else logo_verified_at
            )

            changed = (
                resolved_homepage != existing["homepage_url"]
                or resolved_logo_url != existing["logo_url"]
                or resolved_logo_source != existing["logo_source"]
                or resolved_logo_verified_at != existing["logo_verified_at"]
            )
            if not changed:
                return False

            self._connection.execute(
                """
                UPDATE company_watchlist
                SET homepage_url = ?,
                    logo_url = ?,
                    logo_source = ?,
                    logo_verified_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    resolved_homepage,
                    resolved_logo_url,
                    resolved_logo_source,
                    resolved_logo_verified_at,
                    _utc_now(),
                    company_id,
                ),
            )
            self._connection.commit()
            return True

    async def update_canonical_company_name(self, normalized_company: str, company: str) -> int:
        async with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE canonical_jobs
                SET company = ?
                WHERE normalized_company = ?
                """,
                (company, normalized_company),
            )
            self._connection.commit()
            return int(cursor.rowcount or 0)

    async def get_due_watchlist_companies(
        self,
        *,
        track: str,
        target_type: Optional[str] = None,
        limit: int = 300,
    ) -> List[DbRow]:
        sql = """
            SELECT DISTINCT c.*
            FROM company_watchlist c
            LEFT JOIN company_scan_targets t ON t.company_id = c.id
            WHERE c.active = 1
              AND (c.next_scan_at IS NULL OR c.next_scan_at <= ?)
              AND (c.tracks LIKE '%"both"%' OR c.tracks LIKE ?)
        """
        params: List[Any] = [_utc_now(), f'%"{track}"%']
        if target_type:
            sql += " AND t.target_type = ? AND t.active = 1"
            params.append(target_type)
        sql += " ORDER BY c.tier ASC, c.next_scan_at ASC, c.company ASC LIMIT ?"
        params.append(limit)
        async with self._lock:
            return self._connection.execute(sql, params).fetchall()

    async def mark_watchlist_company_scanned(
        self,
        *,
        company_id: int,
        track: str,
        target_type: str,
        jobs_found: int,
        success: bool,
        next_scan_at: str,
        error: Optional[str] = None,
    ) -> None:
        async with self._lock:
            now = _utc_now()
            self._connection.execute(
                """
                UPDATE company_watchlist
                SET last_scan_at = ?, next_scan_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, next_scan_at, now, company_id),
            )
            self._connection.execute(
                """
                INSERT INTO company_scan_history (
                    company_id, target_type, track, scanned_at, success, jobs_found, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (company_id, target_type, track, now, 1 if success else 0, jobs_found, error),
            )
            self._connection.commit()

    async def upsert_company_scan_target(
        self,
        *,
        company_id: int,
        target_type: str,
        locator: Dict[str, Any],
        confidence: float,
        active: bool = True,
        verified_at: Optional[str] = None,
    ) -> int:
        async with self._lock:
            now = _utc_now()
            locator_json = _json_dump(locator)
            existing = self._connection.execute(
                """
                SELECT id
                FROM company_scan_targets
                WHERE company_id = ? AND target_type = ? AND locator = ?
                """,
                (company_id, target_type, locator_json),
            ).fetchone()
            if existing:
                self._connection.execute(
                    """
                    UPDATE company_scan_targets
                    SET confidence = ?, active = ?, verified_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        confidence,
                        1 if active else 0,
                        verified_at,
                        now,
                        existing["id"],
                    ),
                )
                self._connection.commit()
                return int(existing["id"])

            target_id = self._insert_and_get_id(
                """
                INSERT INTO company_scan_targets (
                    company_id, target_type, locator, confidence, active, verified_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    company_id,
                    target_type,
                    locator_json,
                    confidence,
                    1 if active else 0,
                    verified_at,
                    now,
                    now,
                ),
            )
            self._connection.commit()
            return target_id

    async def get_company_scan_targets(
        self,
        company_id: int,
        *,
        target_type: Optional[str] = None,
    ) -> List[DbRow]:
        sql = "SELECT * FROM company_scan_targets WHERE company_id = ? AND active = 1"
        params: List[Any] = [company_id]
        if target_type:
            sql += " AND target_type = ?"
            params.append(target_type)
        sql += " ORDER BY confidence DESC, updated_at DESC"
        async with self._lock:
            return self._connection.execute(sql, params).fetchall()

    async def get_due_company_scan_targets(
        self,
        *,
        target_type: str,
        track: str,
        limit: int = 500,
    ) -> List[DbRow]:
        async with self._lock:
            return self._connection.execute(
                """
                SELECT t.*, c.company, c.normalized_company, c.careers_url, c.homepage_url,
                       c.country, c.categories, c.tier, c.scan_cadence, c.tracks,
                       c.seed_source, c.notes, c.active as company_active, c.last_scan_at, c.next_scan_at
                FROM company_scan_targets t
                JOIN company_watchlist c ON c.id = t.company_id
                WHERE t.target_type = ?
                  AND t.active = 1
                  AND c.active = 1
                  AND (c.next_scan_at IS NULL OR c.next_scan_at <= ?)
                  AND (c.tracks LIKE '%"both"%' OR c.tracks LIKE ?)
                ORDER BY c.tier ASC, c.next_scan_at ASC, c.company ASC
                LIMIT ?
                """,
                (target_type, _utc_now(), f'%"{track}"%', limit),
            ).fetchall()

    async def create_company_request(
        self,
        *,
        company: str,
        normalized_company: str,
        careers_url: str,
        homepage_url: str,
        requester_user_id: str,
        requester_channel_id: str,
    ) -> int:
        async with self._lock:
            now = _utc_now()
            request_id = self._insert_and_get_id(
                """
                INSERT INTO company_requests (
                    company, normalized_company, careers_url, homepage_url,
                    requester_user_id, requester_channel_id, status, admin_notes,
                    approver_user_id, watchlist_company_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', '', NULL, NULL, ?, ?)
                """,
                (
                    company,
                    normalized_company,
                    careers_url,
                    homepage_url,
                    requester_user_id,
                    requester_channel_id,
                    now,
                    now,
                ),
            )
            self._connection.commit()
            return request_id

    async def find_pending_company_request(
        self,
        *,
        normalized_company: str,
        careers_url: str,
    ) -> Optional[DbRow]:
        async with self._lock:
            return self._connection.execute(
                """
                SELECT *
                FROM company_requests
                WHERE normalized_company = ? AND careers_url = ? AND status = 'pending'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (normalized_company, careers_url),
            ).fetchone()

    async def list_company_requests(
        self,
        *,
        status: Optional[str] = None,
        limit: int = 25,
    ) -> List[DbRow]:
        sql = "SELECT * FROM company_requests"
        params: List[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._lock:
            return self._connection.execute(sql, params).fetchall()

    async def get_company_request(self, request_id: int) -> Optional[DbRow]:
        async with self._lock:
            return self._connection.execute(
                "SELECT * FROM company_requests WHERE id = ?",
                (request_id,),
            ).fetchone()

    async def update_company_request(
        self,
        *,
        request_id: int,
        status: str,
        admin_notes: str,
        approver_user_id: Optional[str],
        watchlist_company_id: Optional[int],
    ) -> None:
        async with self._lock:
            self._connection.execute(
                """
                UPDATE company_requests
                SET status = ?,
                    admin_notes = ?,
                    approver_user_id = ?,
                    watchlist_company_id = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, admin_notes, approver_user_id, watchlist_company_id, _utc_now(), request_id),
            )
            self._connection.commit()

    async def get_job_community_profile(self, user_id: str) -> Optional[DbRow]:
        async with self._lock:
            return self._connection.execute(
                "SELECT * FROM job_user_profiles WHERE user_id = ?",
                (user_id,),
            ).fetchone()

    async def accept_job_community_rules(
        self,
        user_id: str,
        *,
        default_visibility: str = "private",
    ) -> DbRow:
        async with self._lock:
            now = _utc_now()
            existing = self._connection.execute(
                "SELECT * FROM job_user_profiles WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if existing:
                self._connection.execute(
                    """
                    UPDATE job_user_profiles
                    SET updated_at = ?
                    WHERE user_id = ?
                    """,
                    (now, user_id),
                )
            else:
                self._connection.execute(
                    """
                    INSERT INTO job_user_profiles (user_id, visibility, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_id, default_visibility, now, now),
                )
            self._connection.commit()
            return self._connection.execute(
                "SELECT * FROM job_user_profiles WHERE user_id = ?",
                (user_id,),
            ).fetchone()

    async def update_job_community_visibility(
        self,
        user_id: str,
        visibility: str,
    ) -> Optional[DbRow]:
        async with self._lock:
            existing = self._connection.execute(
                "SELECT * FROM job_user_profiles WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if not existing:
                return None
            self._connection.execute(
                """
                UPDATE job_user_profiles
                SET visibility = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (visibility, _utc_now(), user_id),
            )
            self._connection.commit()
            return self._connection.execute(
                "SELECT * FROM job_user_profiles WHERE user_id = ?",
                (user_id,),
            ).fetchone()

    async def get_job_application_status(
        self,
        *,
        user_id: str,
        company_id: int,
    ) -> Optional[DbRow]:
        async with self._lock:
            return self._connection.execute(
                f"""
                SELECT
                    s.id,
                    s.user_id,
                    s.company_id,
                    s.status,
                    s.created_at,
                    s.updated_at,
                    c.company,
                    c.normalized_company,
                    COALESCE(latest_note_meta.note_count, 0) AS note_count,
                    latest_note.body AS latest_note,
                    latest_note.created_at AS latest_note_at
                FROM job_application_statuses s
                JOIN company_watchlist c ON c.id = s.company_id
                {self._latest_process_note_join_sql()}
                WHERE s.user_id = ? AND s.company_id = ?
                LIMIT 1
                """,
                (user_id, company_id),
            ).fetchone()

    async def upsert_job_application_status(
        self,
        *,
        user_id: str,
        company_id: int,
        status: str,
        notes: Optional[str] = None,
    ) -> DbRow:
        async with self._lock:
            now = _utc_now()
            existing = self._connection.execute(
                """
                SELECT id
                FROM job_application_statuses
                WHERE user_id = ? AND company_id = ?
                """,
                (user_id, company_id),
            ).fetchone()
            if existing:
                self._connection.execute(
                    """
                    UPDATE job_application_statuses
                    SET status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, now, existing["id"]),
                )
            else:
                self._connection.execute(
                    """
                    INSERT INTO job_application_statuses (
                        user_id, company_id, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (user_id, company_id, status, now, now),
                )
                existing = self._connection.execute(
                    """
                    SELECT id
                    FROM job_application_statuses
                    WHERE user_id = ? AND company_id = ?
                    """,
                    (user_id, company_id),
                ).fetchone()
            if notes is not None and notes.strip():
                self._insert_process_note_locked(
                    user_id=user_id,
                    company_id=company_id,
                    body=notes.strip(),
                )
            self._connection.commit()
            return self._connection.execute(
                f"""
                SELECT
                    s.id,
                    s.user_id,
                    s.company_id,
                    s.status,
                    s.created_at,
                    s.updated_at,
                    c.company,
                    c.normalized_company,
                    COALESCE(latest_note_meta.note_count, 0) AS note_count,
                    latest_note.body AS latest_note,
                    latest_note.created_at AS latest_note_at
                FROM job_application_statuses s
                JOIN company_watchlist c ON c.id = s.company_id
                {self._latest_process_note_join_sql()}
                WHERE s.user_id = ? AND s.company_id = ?
                LIMIT 1
                """,
                (user_id, company_id),
            ).fetchone()

    async def add_process_note(
        self,
        *,
        user_id: str,
        company_id: int,
        body: str,
    ) -> DbRow:
        async with self._lock:
            row = self._insert_process_note_locked(
                user_id=user_id,
                company_id=company_id,
                body=body.strip(),
            )
            self._connection.commit()
            return row

    async def list_job_application_statuses_for_user(
        self,
        user_id: str,
        *,
        limit: int = 25,
    ) -> List[DbRow]:
        async with self._lock:
            return self._connection.execute(
                f"""
                SELECT
                    s.id,
                    s.user_id,
                    s.company_id,
                    s.status,
                    s.created_at,
                    s.updated_at,
                    c.company,
                    c.normalized_company,
                    COALESCE(latest_note_meta.note_count, 0) AS note_count,
                    latest_note.body AS latest_note,
                    latest_note.created_at AS latest_note_at
                FROM job_application_statuses s
                JOIN company_watchlist c ON c.id = s.company_id
                {self._latest_process_note_join_sql()}
                WHERE s.user_id = ?
                ORDER BY s.updated_at DESC, lower(c.company) ASC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()

    async def list_job_peers(
        self,
        *,
        company_id: int,
        requester_user_id: str,
        include_matched: bool,
        limit: int = 25,
    ) -> List[DbRow]:
        visibility_values = ["open"]
        if include_matched:
            visibility_values.append("matched")
        placeholders = ", ".join("?" for _ in visibility_values)
        params: List[Any] = [company_id, requester_user_id, *visibility_values, limit]
        async with self._lock:
            return self._connection.execute(
                f"""
                SELECT
                       s.id,
                       s.user_id,
                       s.company_id,
                       s.status,
                       s.created_at,
                       s.updated_at,
                       p.visibility,
                       c.company,
                       c.normalized_company,
                       COALESCE(latest_note_meta.note_count, 0) AS note_count,
                       latest_note.body AS latest_note,
                       latest_note.created_at AS latest_note_at
                FROM job_application_statuses s
                JOIN job_user_profiles p ON p.user_id = s.user_id
                JOIN company_watchlist c ON c.id = s.company_id
                {self._latest_process_note_join_sql()}
                WHERE s.company_id = ?
                  AND s.user_id <> ?
                  AND s.status <> 'hide'
                  AND p.visibility IN ({placeholders})
                ORDER BY
                    CASE p.visibility
                        WHEN 'open' THEN 0
                        WHEN 'matched' THEN 1
                        ELSE 2
                    END,
                    s.updated_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()

    async def list_process_notes(
        self,
        *,
        company_id: int,
        requester_user_id: str,
        include_matched: bool,
        limit: int = 50,
    ) -> List[DbRow]:
        visibility_values = ["open"]
        if include_matched:
            visibility_values.append("matched")
        placeholders = ", ".join("?" for _ in visibility_values)
        params: List[Any] = [
            company_id,
            requester_user_id,
            requester_user_id,
            *visibility_values,
            limit,
        ]
        async with self._lock:
            return self._connection.execute(
                f"""
                SELECT
                    n.id,
                    n.user_id,
                    n.company_id,
                    n.body,
                    n.created_at,
                    n.updated_at,
                    s.status,
                    p.visibility,
                    c.company,
                    c.normalized_company
                FROM process_notes n
                JOIN job_application_statuses s
                    ON s.user_id = n.user_id
                   AND s.company_id = n.company_id
                JOIN job_user_profiles p
                    ON p.user_id = n.user_id
                JOIN company_watchlist c
                    ON c.id = n.company_id
                WHERE n.company_id = ?
                  AND s.status <> 'hide'
                  AND (
                      n.user_id = ?
                      OR (n.user_id <> ? AND p.visibility IN ({placeholders}))
                  )
                ORDER BY n.created_at DESC, n.id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()

    async def upsert_process_role_scope(
        self,
        *,
        company_id: Optional[int],
        canonical_job_id: Optional[int],
        company: str,
        normalized_company: str,
        role_title: str,
        normalized_role_title: str,
        track: str,
        season_term: str,
        canonical_job_url: str = "",
        scope_origin: str = "manual",
    ) -> DbRow:
        role_key = "|".join(
            [
                normalized_company,
                normalized_role_title,
                track,
                season_term,
            ]
        )
        async with self._lock:
            now = _utc_now()
            existing = self._connection.execute(
                """
                SELECT *
                FROM process_role_scopes
                WHERE role_key = ?
                LIMIT 1
                """,
                (role_key,),
            ).fetchone()
            if existing:
                self._connection.execute(
                    """
                    UPDATE process_role_scopes
                    SET company_id = COALESCE(company_id, ?),
                        canonical_job_id = COALESCE(canonical_job_id, ?),
                        company = ?,
                        canonical_job_url = CASE
                            WHEN canonical_job_url = '' THEN ?
                            ELSE canonical_job_url
                        END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        company_id,
                        canonical_job_id,
                        company,
                        canonical_job_url,
                        now,
                        existing["id"],
                    ),
                )
                self._connection.commit()
                return self._connection.execute(
                    "SELECT * FROM process_role_scopes WHERE id = ?",
                    (existing["id"],),
                ).fetchone()

            scope_id = self._insert_and_get_id(
                """
                INSERT INTO process_role_scopes (
                    company_id,
                    canonical_job_id,
                    legacy_company_id,
                    company,
                    normalized_company,
                    role_title,
                    normalized_role_title,
                    track,
                    season_term,
                    canonical_job_url,
                    role_key,
                    scope_origin,
                    created_at,
                    updated_at
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    company_id,
                    canonical_job_id,
                    company,
                    normalized_company,
                    role_title,
                    normalized_role_title,
                    track,
                    season_term,
                    canonical_job_url,
                    role_key,
                    scope_origin,
                    now,
                    now,
                ),
            )
            self._connection.commit()
            return self._connection.execute(
                "SELECT * FROM process_role_scopes WHERE id = ?",
                (scope_id,),
            ).fetchone()

    async def get_process_role_scope(self, scope_id: int) -> Optional[DbRow]:
        async with self._lock:
            return self._connection.execute(
                "SELECT * FROM process_role_scopes WHERE id = ?",
                (scope_id,),
            ).fetchone()

    async def list_process_role_scopes_for_company(
        self,
        *,
        company_id: int,
        limit: int = 100,
    ) -> List[DbRow]:
        async with self._lock:
            return self._connection.execute(
                """
                SELECT *
                FROM process_role_scopes
                WHERE company_id = ?
                ORDER BY
                    CASE WHEN track = 'legacy' THEN 1 ELSE 0 END,
                    updated_at DESC,
                    role_title ASC
                LIMIT ?
                """,
                (company_id, limit),
            ).fetchall()

    async def find_process_role_scopes(
        self,
        *,
        normalized_company: str,
        normalized_role_title: str = "",
        season_term: str = "",
        track: Optional[str] = None,
        limit: int = 25,
    ) -> List[DbRow]:
        sql = """
            SELECT *
            FROM process_role_scopes
            WHERE normalized_company = ?
        """
        params: List[Any] = [normalized_company]
        if normalized_role_title:
            sql += " AND normalized_role_title = ?"
            params.append(normalized_role_title)
        if season_term:
            sql += " AND season_term = ?"
            params.append(season_term)
        if track:
            sql += " AND track = ?"
            params.append(track)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        async with self._lock:
            return self._connection.execute(sql, params).fetchall()

    async def get_process_role_status(
        self,
        *,
        user_id: str,
        scope_id: int,
    ) -> Optional[DbRow]:
        async with self._lock:
            return self._connection.execute(
                f"""
                SELECT
                    s.id,
                    s.user_id,
                    s.scope_id,
                    s.status,
                    s.created_at,
                    s.updated_at,
                    scope.company,
                    scope.normalized_company,
                    scope.role_title,
                    scope.track,
                    scope.season_term,
                    scope.canonical_job_url,
                    COALESCE(latest_role_note_meta.note_count, 0) AS note_count,
                    latest_role_note.body AS latest_note,
                    latest_role_note.created_at AS latest_note_at
                FROM process_role_statuses s
                JOIN process_role_scopes scope ON scope.id = s.scope_id
                {self._latest_role_note_join_sql()}
                WHERE s.user_id = ? AND s.scope_id = ?
                LIMIT 1
                """,
                (user_id, scope_id),
            ).fetchone()

    async def upsert_process_role_status(
        self,
        *,
        user_id: str,
        scope_id: int,
        status: str,
    ) -> DbRow:
        async with self._lock:
            now = _utc_now()
            existing = self._connection.execute(
                """
                SELECT id
                FROM process_role_statuses
                WHERE user_id = ? AND scope_id = ?
                """,
                (user_id, scope_id),
            ).fetchone()
            if existing:
                self._connection.execute(
                    """
                    UPDATE process_role_statuses
                    SET status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, now, existing["id"]),
                )
            else:
                self._connection.execute(
                    """
                    INSERT INTO process_role_statuses (
                        user_id, scope_id, legacy_status_id, status, created_at, updated_at
                    ) VALUES (?, ?, NULL, ?, ?, ?)
                    """,
                    (user_id, scope_id, status, now, now),
                )
            self._connection.execute(
                """
                UPDATE process_role_scopes
                SET updated_at = ?
                WHERE id = ?
                """,
                (now, scope_id),
            )
            self._connection.commit()
            return self._connection.execute(
                f"""
                SELECT
                    s.id,
                    s.user_id,
                    s.scope_id,
                    s.status,
                    s.created_at,
                    s.updated_at,
                    scope.company,
                    scope.normalized_company,
                    scope.role_title,
                    scope.track,
                    scope.season_term,
                    scope.canonical_job_url,
                    COALESCE(latest_role_note_meta.note_count, 0) AS note_count,
                    latest_role_note.body AS latest_note,
                    latest_role_note.created_at AS latest_note_at
                FROM process_role_statuses s
                JOIN process_role_scopes scope ON scope.id = s.scope_id
                {self._latest_role_note_join_sql()}
                WHERE s.user_id = ? AND s.scope_id = ?
                LIMIT 1
                """,
                (user_id, scope_id),
            ).fetchone()

    async def list_process_role_statuses_for_user(
        self,
        user_id: str,
        *,
        active_only: bool = False,
        limit: int = 100,
    ) -> List[DbRow]:
        sql = f"""
            SELECT
                s.id,
                s.user_id,
                s.scope_id,
                s.status,
                s.created_at,
                s.updated_at,
                scope.company,
                scope.normalized_company,
                scope.role_title,
                scope.track,
                scope.season_term,
                scope.canonical_job_url,
                COALESCE(latest_role_note_meta.note_count, 0) AS note_count,
                latest_role_note.body AS latest_note,
                latest_role_note.created_at AS latest_note_at
            FROM process_role_statuses s
            JOIN process_role_scopes scope ON scope.id = s.scope_id
            {self._latest_role_note_join_sql()}
            WHERE s.user_id = ?
        """
        params: List[Any] = [user_id]
        if active_only:
            sql += " AND s.status NOT IN ('hide', 'rejected', 'offer')"
        sql += " ORDER BY s.updated_at DESC, lower(scope.company) ASC LIMIT ?"
        params.append(limit)
        async with self._lock:
            return self._connection.execute(sql, params).fetchall()

    async def list_process_role_peers(
        self,
        *,
        scope_id: int,
        requester_user_id: str,
        include_matched: bool,
        limit: int = 100,
    ) -> List[DbRow]:
        visibility_values = ["open"]
        if include_matched:
            visibility_values.append("matched")
        placeholders = ", ".join("?" for _ in visibility_values)
        params: List[Any] = [scope_id, requester_user_id, *visibility_values, limit]
        async with self._lock:
            return self._connection.execute(
                f"""
                SELECT
                    s.id,
                    s.user_id,
                    s.scope_id,
                    s.status,
                    s.created_at,
                    s.updated_at,
                    p.visibility,
                    scope.company,
                    scope.normalized_company,
                    scope.role_title,
                    scope.track,
                    scope.season_term,
                    COALESCE(latest_role_note_meta.note_count, 0) AS note_count,
                    latest_role_note.body AS latest_note,
                    latest_role_note.created_at AS latest_note_at
                FROM process_role_statuses s
                JOIN process_role_scopes scope ON scope.id = s.scope_id
                JOIN job_user_profiles p ON p.user_id = s.user_id
                {self._latest_role_note_join_sql()}
                WHERE s.scope_id = ?
                  AND s.user_id <> ?
                  AND s.status <> 'hide'
                  AND p.visibility IN ({placeholders})
                ORDER BY
                    CASE p.visibility
                        WHEN 'open' THEN 0
                        WHEN 'matched' THEN 1
                        ELSE 2
                    END,
                    s.updated_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()

    async def add_process_role_note(
        self,
        *,
        status_id: int,
        scope_id: int,
        user_id: str,
        body: str,
    ) -> DbRow:
        async with self._lock:
            now = _utc_now()
            note_id = self._insert_and_get_id(
                """
                INSERT INTO process_role_notes (
                    status_id,
                    scope_id,
                    user_id,
                    legacy_note_id,
                    body,
                    moderation_status,
                    report_count,
                    deleted_at,
                    deleted_by_user_id,
                    deleted_reason,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, NULL, ?, 'active', 0, NULL, NULL, '', ?, ?)
                """,
                (status_id, scope_id, user_id, body.strip(), now, now),
            )
            self._connection.execute(
                """
                UPDATE process_role_statuses
                SET updated_at = ?
                WHERE id = ?
                """,
                (now, status_id),
            )
            self._connection.execute(
                """
                UPDATE process_role_scopes
                SET updated_at = ?
                WHERE id = ?
                """,
                (now, scope_id),
            )
            self._connection.commit()
            return self._connection.execute(
                "SELECT * FROM process_role_notes WHERE id = ?",
                (note_id,),
            ).fetchone()

    async def list_process_role_notes(
        self,
        *,
        scope_id: int,
        requester_user_id: str,
        include_matched: bool,
        limit: int = 100,
    ) -> List[DbRow]:
        visibility_values = ["open"]
        if include_matched:
            visibility_values.append("matched")
        placeholders = ", ".join("?" for _ in visibility_values)
        params: List[Any] = [
            scope_id,
            requester_user_id,
            requester_user_id,
            *visibility_values,
            limit,
        ]
        async with self._lock:
            return self._connection.execute(
                f"""
                SELECT
                    n.id,
                    n.status_id,
                    n.scope_id,
                    n.user_id,
                    n.body,
                    n.moderation_status,
                    n.report_count,
                    n.created_at,
                    n.updated_at,
                    s.status,
                    p.visibility,
                    scope.company,
                    scope.role_title,
                    scope.track,
                    scope.season_term
                FROM process_role_notes n
                JOIN process_role_statuses s
                    ON s.id = n.status_id
                JOIN job_user_profiles p
                    ON p.user_id = n.user_id
                JOIN process_role_scopes scope
                    ON scope.id = n.scope_id
                WHERE n.scope_id = ?
                  AND n.moderation_status = 'active'
                  AND s.status <> 'hide'
                  AND (
                      n.user_id = ?
                      OR (n.user_id <> ? AND p.visibility IN ({placeholders}))
                  )
                ORDER BY n.created_at DESC, n.id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()

    async def get_process_role_note(self, note_id: int) -> Optional[DbRow]:
        async with self._lock:
            return self._connection.execute(
                """
                SELECT
                    n.*,
                    s.status,
                    scope.company,
                    scope.role_title,
                    scope.track,
                    scope.season_term
                FROM process_role_notes n
                JOIN process_role_statuses s ON s.id = n.status_id
                JOIN process_role_scopes scope ON scope.id = n.scope_id
                WHERE n.id = ?
                LIMIT 1
                """,
                (note_id,),
            ).fetchone()

    async def report_process_role_note(
        self,
        *,
        note_id: int,
        reporter_user_id: str,
        reason: str,
        auto_hide_threshold: int = 2,
    ) -> Optional[DbRow]:
        async with self._lock:
            note = self._connection.execute(
                "SELECT * FROM process_role_notes WHERE id = ?",
                (note_id,),
            ).fetchone()
            if not note:
                return None
            self._connection.execute(
                """
                INSERT OR IGNORE INTO process_note_reports (
                    note_id, reporter_user_id, reason, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (note_id, reporter_user_id, reason.strip(), _utc_now()),
            )
            report_count = self._connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM process_note_reports
                WHERE note_id = ?
                """,
                (note_id,),
            ).fetchone()["count"]
            moderation_status = "hidden" if report_count >= auto_hide_threshold else note["moderation_status"]
            self._connection.execute(
                """
                UPDATE process_role_notes
                SET report_count = ?, moderation_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (report_count, moderation_status, _utc_now(), note_id),
            )
            self._connection.commit()
            return self._connection.execute(
                "SELECT * FROM process_role_notes WHERE id = ?",
                (note_id,),
            ).fetchone()

    async def delete_process_role_note(
        self,
        *,
        note_id: int,
        actor_user_id: str,
        allow_moderator_delete: bool,
        reason: str = "",
    ) -> Optional[DbRow]:
        async with self._lock:
            note = self._connection.execute(
                "SELECT * FROM process_role_notes WHERE id = ?",
                (note_id,),
            ).fetchone()
            if not note:
                return None
            if note["user_id"] != actor_user_id and not allow_moderator_delete:
                return None
            self._connection.execute(
                """
                UPDATE process_role_notes
                SET moderation_status = 'deleted',
                    deleted_at = ?,
                    deleted_by_user_id = ?,
                    deleted_reason = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (_utc_now(), actor_user_id, reason.strip(), _utc_now(), note_id),
            )
            self._connection.commit()
            return self._connection.execute(
                "SELECT * FROM process_role_notes WHERE id = ?",
                (note_id,),
            ).fetchone()

    async def record_process_activity(
        self,
        *,
        user_id: str,
        event_type: str,
        scope_id: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        async with self._lock:
            self._connection.execute(
                """
                INSERT INTO process_activity_events (
                    user_id, scope_id, event_type, metadata, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, scope_id, event_type, _json_dump(metadata or {}), _utc_now()),
            )
            self._connection.commit()

    async def count_recent_process_activity(
        self,
        *,
        user_id: str,
        event_type: str,
        since_seconds: int,
    ) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=since_seconds)).isoformat()
        async with self._lock:
            row = self._connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM process_activity_events
                WHERE user_id = ? AND event_type = ? AND created_at >= ?
                """,
                (user_id, event_type, cutoff),
            ).fetchone()
        return int(row["count"] if row else 0)

    async def get_canonical_by_fingerprint_keys(self, keys: Iterable[str]) -> Optional[CanonicalJob]:
        values = [key for key in keys if key]
        if not values:
            return None
        placeholders = ", ".join("?" for _ in values)
        async with self._lock:
            cursor = self._connection.execute(
                f"""
                SELECT c.*
                FROM canonical_jobs c
                JOIN fingerprints f ON f.canonical_id = c.id
                WHERE f.fingerprint_key IN ({placeholders})
                LIMIT 1
                """,
                values,
            )
            row = cursor.fetchone()
        return self._row_to_canonical(row) if row else None

    async def get_canonical_fingerprint_keys(self, canonical_id: int) -> List[str]:
        async with self._lock:
            cursor = self._connection.execute(
                "SELECT fingerprint_key FROM fingerprints WHERE canonical_id = ?",
                (canonical_id,),
            )
            return [row["fingerprint_key"] for row in cursor.fetchall()]

    async def get_fuzzy_candidates(
        self,
        *,
        track: str,
        normalized_company: str,
        season_term: Optional[str],
    ) -> List[CanonicalJob]:
        query = """
            SELECT *
            FROM canonical_jobs
            WHERE track = ? AND normalized_company = ?
        """
        params: List[Any] = [track, normalized_company]
        if season_term:
            query += " AND (season_term = ? OR season_term IS NULL)"
            params.append(season_term)
        query += " ORDER BY last_seen DESC LIMIT 100"
        async with self._lock:
            cursor = self._connection.execute(query, params)
            rows = cursor.fetchall()
        return [self._row_to_canonical(row) for row in rows]

    async def get_posted_candidates(
        self,
        *,
        channel_id: int,
        track: str,
        normalized_company: str,
    ) -> List[CanonicalJob]:
        async with self._lock:
            cursor = self._connection.execute(
                """
                SELECT DISTINCT c.*
                FROM canonical_jobs c
                JOIN discord_posts d ON d.canonical_id = c.id
                WHERE d.channel_id = ? AND c.track = ? AND c.normalized_company = ?
                ORDER BY c.last_seen DESC
                LIMIT 100
                """,
                (str(channel_id), track, normalized_company),
            )
            rows = cursor.fetchall()
        return [self._row_to_canonical(row) for row in rows]

    async def save_job(
        self,
        job: JobPosting,
        canonical_id: Optional[int] = None,
    ) -> tuple[int, bool, bool]:
        async with self._lock:
            if canonical_id is None:
                now = _utc_now()
                canonical_id = self._insert_and_get_id(
                    """
                    INSERT INTO canonical_jobs (
                        track, title, company, location, summary, canonical_url,
                        season_term, employment_type, remote_status, status,
                        normalized_company, normalized_title, normalized_locations,
                        dedupe_fingerprint, source_names, provenance_tags, first_seen, last_seen
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job.track,
                        job.title,
                        job.company,
                        job.location_display,
                        job.summary,
                        job.canonical_url or job.url,
                        job.season_term,
                        job.employment_type,
                        job.remote_status,
                        job.status,
                        job.normalized_company,
                        job.normalized_title,
                        _json_dump(job.normalized_locations),
                        job.dedupe_fingerprint,
                        _json_dump([job.source_name]),
                        _json_dump(job.provenance_tags),
                        now,
                        now,
                    ),
                )
                created = True
                refresh_required = False
            else:
                row = self._connection.execute(
                    "SELECT source_names, provenance_tags, summary FROM canonical_jobs WHERE id = ?",
                    (canonical_id,),
                ).fetchone()
                existing_sources = json.loads(row["source_names"]) if row else []
                existing_provenance = json.loads(row["provenance_tags"]) if row else []
                existing_summary = row["summary"] if row else ""
                merged_sources = sorted(set(existing_sources + [job.source_name]))
                merged_provenance = sorted(set(existing_provenance + list(job.provenance_tags)))
                resolved_summary = existing_summary or job.summary
                refresh_required = (
                    merged_sources != existing_sources
                    or merged_provenance != existing_provenance
                    or resolved_summary != existing_summary
                )
                self._connection.execute(
                    """
                    UPDATE canonical_jobs
                    SET last_seen = ?,
                        summary = COALESCE(NULLIF(summary, ''), ?),
                        source_names = ?,
                        provenance_tags = ?,
                        status = ?
                    WHERE id = ?
                    """,
                    (
                        _utc_now(),
                        job.summary,
                        _json_dump(merged_sources),
                        _json_dump(merged_provenance),
                        job.status,
                        canonical_id,
                    ),
                )
                created = False

            variant_key = self._variant_key(job)
            self._connection.execute(
                """
                INSERT OR IGNORE INTO job_variants (
                    canonical_id, track, source_name, source_job_id, url, canonical_url,
                    title, company, location, summary, posted_date, date_found, season_term,
                    employment_type, remote_status, status, normalized_company,
                    normalized_title, normalized_locations, external_ids, alternate_urls,
                    provenance_tags, raw_data, row_hash, variant_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_id,
                    job.track,
                    job.source_name,
                    job.source_job_id,
                    job.url,
                    job.canonical_url or job.url,
                    job.title,
                    job.company,
                    job.location_display,
                    job.summary,
                    job.posted_date,
                    job.date_found.isoformat(),
                    job.season_term,
                    job.employment_type,
                    job.remote_status,
                    job.status,
                    job.normalized_company,
                    job.normalized_title,
                    _json_dump(job.normalized_locations),
                    _json_dump(job.external_ids),
                    _json_dump(job.alternate_urls),
                    _json_dump(job.provenance_tags),
                    _json_dump(job.raw_data),
                    job.row_hash,
                    variant_key,
                ),
            )

            self._store_fingerprints(canonical_id, job)
            self._connection.commit()
            return canonical_id, created, refresh_required

    def _store_fingerprints(self, canonical_id: int, job: JobPosting) -> None:
        keys = {f"strong:{job.dedupe_fingerprint}"}
        if job.canonical_url:
            keys.add(f"url:{job.canonical_url}")
        for name, value in job.external_ids.items():
            keys.add(f"external:{name}:{value}")
        now = _utc_now()
        for key in keys:
            fingerprint_type = key.split(":", 1)[0]
            self._connection.execute(
                """
                INSERT OR IGNORE INTO fingerprints (fingerprint_key, canonical_id, fingerprint_type, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (key, canonical_id, fingerprint_type, now),
            )

    async def record_dedupe_match(
        self,
        canonical_id: int,
        variant_key: str,
        reason: str,
        score: Optional[float],
    ) -> None:
        async with self._lock:
            self._connection.execute(
                """
                INSERT INTO dedupe_matches (canonical_id, variant_key, reason, score, matched_on, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (canonical_id, variant_key, reason, score, variant_key, _utc_now()),
            )
            self._connection.commit()

    async def record_source_run(
        self,
        *,
        source_name: str,
        track: str,
        status: str,
        jobs_fetched: int,
        jobs_filtered: int,
        jobs_new: int,
        jobs_duplicates: int,
        metadata: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        async with self._lock:
            now = _utc_now()
            self._connection.execute(
                """
                INSERT INTO source_runs (
                    source_name, track, started_at, finished_at, status, jobs_fetched,
                    jobs_filtered, jobs_new, jobs_duplicates, metadata, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_name,
                    track,
                    now,
                    now,
                    status,
                    jobs_fetched,
                    jobs_filtered,
                    jobs_new,
                    jobs_duplicates,
                    _json_dump(metadata or {}),
                    error,
                ),
            )
            if error:
                self._connection.execute(
                    """
                    INSERT INTO source_errors (source_name, occurred_at, error, context)
                    VALUES (?, ?, ?, ?)
                    """,
                    (source_name, now, error, _json_dump(metadata or {})),
                )
            self._connection.commit()

    async def is_posted(self, canonical_id: int, channel_id: int) -> bool:
        async with self._lock:
            cursor = self._connection.execute(
                "SELECT 1 FROM discord_posts WHERE canonical_id = ? AND channel_id = ?",
                (canonical_id, str(channel_id)),
            )
            return cursor.fetchone() is not None

    async def mark_posted(
        self,
        canonical_id: int,
        track: str,
        channel_id: int,
        message_id: int,
    ) -> None:
        async with self._lock:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO discord_posts (canonical_id, track, channel_id, message_id, posted_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (canonical_id, track, str(channel_id), str(message_id), _utc_now()),
            )
            self._connection.commit()

    async def get_post_records_for_canonical(self, canonical_id: int) -> List[DbRow]:
        async with self._lock:
            cursor = self._connection.execute(
                """
                SELECT channel_id, message_id, track, posted_at
                FROM discord_posts
                WHERE canonical_id = ?
                ORDER BY posted_at ASC
                """,
                (canonical_id,),
            )
            return cursor.fetchall()

    async def get_canonical_jobs(self, canonical_ids: Iterable[int]) -> List[CanonicalJob]:
        values = [value for value in canonical_ids if value is not None]
        if not values:
            return []
        placeholders = ", ".join("?" for _ in values)
        async with self._lock:
            cursor = self._connection.execute(
                f"SELECT * FROM canonical_jobs WHERE id IN ({placeholders}) ORDER BY first_seen ASC",
                values,
            )
            rows = cursor.fetchall()
        return [self._row_to_canonical(row) for row in rows]

    async def get_stats(self) -> Dict[str, int]:
        async with self._lock:
            canonical_count = self._connection.execute(
                "SELECT COUNT(*) AS count FROM canonical_jobs"
            ).fetchone()["count"]
            variant_count = self._connection.execute(
                "SELECT COUNT(*) AS count FROM job_variants"
            ).fetchone()["count"]
            posted_count = self._connection.execute(
                "SELECT COUNT(DISTINCT canonical_id) AS count FROM discord_posts"
            ).fetchone()["count"]
            dedupe_count = self._connection.execute(
                "SELECT COUNT(*) AS count FROM dedupe_matches"
            ).fetchone()["count"]
        return {
            "total_canonical_jobs": canonical_count,
            "total_jobs_found": variant_count,
            "total_posted": posted_count,
            "total_deduplicated": dedupe_count,
        }

    async def get_recent_jobs(self, limit: int = 10) -> List[CanonicalJob]:
        async with self._lock:
            cursor = self._connection.execute(
                "SELECT * FROM canonical_jobs ORDER BY first_seen DESC LIMIT ?",
                (limit,),
            )
            rows = cursor.fetchall()
        return [self._row_to_canonical(row) for row in rows]

    async def find_by_url(self, canonical_url: str) -> Optional[CanonicalJob]:
        return await self.get_canonical_by_fingerprint_keys([f"url:{canonical_url}"])

    async def get_jobs_for_summary(self, hours: int) -> List[CanonicalJob]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        async with self._lock:
            cursor = self._connection.execute(
                "SELECT * FROM canonical_jobs WHERE first_seen >= ? ORDER BY first_seen DESC",
                (cutoff,),
            )
            rows = cursor.fetchall()
        return [self._row_to_canonical(row) for row in rows]

    async def get_posted_canonical_jobs_for_companies(
        self,
        normalized_companies: Iterable[str],
    ) -> List[CanonicalJob]:
        values = sorted({value for value in normalized_companies if value})
        if not values:
            return []
        placeholders = ", ".join("?" for _ in values)
        async with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT DISTINCT c.*
                FROM canonical_jobs c
                JOIN discord_posts d ON d.canonical_id = c.id
                WHERE c.normalized_company IN ({placeholders})
                ORDER BY c.last_seen DESC
                """,
                values,
            ).fetchall()
        return [self._row_to_canonical(row) for row in rows]

    async def search_text(self, query: str, limit: int = 25) -> List[CanonicalJob]:
        like = f"%{query.lower()}%"
        async with self._lock:
            cursor = self._connection.execute(
                """
                SELECT *
                FROM canonical_jobs
                WHERE lower(title) LIKE ? OR lower(company) LIKE ? OR lower(location) LIKE ?
                ORDER BY last_seen DESC
                LIMIT ?
                """,
                (like, like, like, limit),
            )
            rows = cursor.fetchall()
        return [self._row_to_canonical(row) for row in rows]

    def _variant_key(self, job: JobPosting) -> str:
        return job.row_hash or "|".join(
            [
                job.source_name,
                job.source_job_id or "",
                job.canonical_url or job.url,
                job.normalized_company,
                job.normalized_title,
                job.canonical_location,
            ]
        )

    def _row_to_canonical(self, row: Optional[DbRow]) -> CanonicalJob:
        if row is None:
            raise ValueError("row cannot be None")
        return CanonicalJob(
            id=row["id"],
            track=row["track"],
            title=row["title"],
            company=row["company"],
            location=row["location"],
            canonical_url=row["canonical_url"],
            summary=row["summary"] or "",
            season_term=row["season_term"],
            employment_type=row["employment_type"],
            remote_status=row["remote_status"] or "unknown",
            status=row["status"],
            normalized_company=row["normalized_company"],
            normalized_title=row["normalized_title"],
            normalized_locations=json.loads(row["normalized_locations"] or "[]"),
            dedupe_fingerprint=row["dedupe_fingerprint"],
            source_names=json.loads(row["source_names"] or "[]"),
            provenance_tags=json.loads(row["provenance_tags"] or "[]"),
            first_seen=datetime.fromisoformat(row["first_seen"]),
            last_seen=datetime.fromisoformat(row["last_seen"]),
        )
