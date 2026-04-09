from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from job_bot.models import CanonicalJob, JobPosting
from job_bot.utils.normalization import normalize_company_name


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True)


class Database:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")

    async def initialize(self) -> None:
        async with self._lock:
            self._connection.executescript(
                """
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
                """
            )
            self._ensure_column("canonical_jobs", "provenance_tags", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column("job_variants", "provenance_tags", "TEXT NOT NULL DEFAULT '[]'")
            self._connection.commit()

    async def close(self) -> None:
        async with self._lock:
            self._connection.close()

    def _ensure_column(self, table_name: str, column_name: str, column_sql: str) -> None:
        columns = {
            row["name"]
            for row in self._connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name in columns:
            return
        self._connection.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}"
        )

    async def get_github_state(self, repository: str, path: str) -> Optional[sqlite3.Row]:
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

    async def get_board_seeds(self, source_name: str, track: Optional[str] = None) -> List[sqlite3.Row]:
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
    ) -> int:
        async with self._lock:
            now = _utc_now()
            existing = self._connection.execute(
                """
                SELECT id, categories, tracks, notes
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
                self._connection.execute(
                    """
                    UPDATE company_watchlist
                    SET company = ?,
                        homepage_url = ?,
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
                        homepage_url,
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

            cursor = self._connection.execute(
                """
                INSERT INTO company_watchlist (
                    company, normalized_company, careers_url, homepage_url, country,
                    categories, tier, scan_cadence, tracks, seed_source, notes,
                    active, next_scan_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    company,
                    normalized_company,
                    careers_url,
                    homepage_url,
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
            return int(cursor.lastrowid)

    async def list_watchlist_companies(
        self,
        *,
        query: str = "",
        active_only: bool = True,
        limit: int = 50,
    ) -> List[sqlite3.Row]:
        sql = "SELECT * FROM company_watchlist WHERE 1 = 1"
        params: List[Any] = []
        if active_only:
            sql += " AND active = 1"
        if query:
            like = f"%{query.lower()}%"
            sql += " AND (lower(company) LIKE ? OR lower(tier) LIKE ? OR lower(categories) LIKE ?)"
            params.extend([like, like, like])
        sql += " ORDER BY company COLLATE NOCASE ASC, tier ASC LIMIT ?"
        params.append(limit)
        async with self._lock:
            return self._connection.execute(sql, params).fetchall()

    async def get_watchlist_company_by_id(self, company_id: int) -> Optional[sqlite3.Row]:
        async with self._lock:
            return self._connection.execute(
                "SELECT * FROM company_watchlist WHERE id = ?",
                (company_id,),
            ).fetchone()

    async def find_watchlist_company(self, query: str) -> Optional[sqlite3.Row]:
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

    async def get_company_branding(self, normalized_company: str) -> Optional[sqlite3.Row]:
        async with self._lock:
            return self._connection.execute(
                """
                SELECT company, normalized_company, homepage_url, careers_url
                FROM company_watchlist
                WHERE normalized_company = ?
                ORDER BY
                    CASE WHEN homepage_url <> '' THEN 0 ELSE 1 END,
                    active DESC,
                    updated_at DESC
                LIMIT 1
                """,
                (normalized_company,),
            ).fetchone()

    async def get_due_watchlist_companies(
        self,
        *,
        track: str,
        target_type: Optional[str] = None,
        limit: int = 300,
    ) -> List[sqlite3.Row]:
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

            cursor = self._connection.execute(
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
            return int(cursor.lastrowid)

    async def get_company_scan_targets(
        self,
        company_id: int,
        *,
        target_type: Optional[str] = None,
    ) -> List[sqlite3.Row]:
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
    ) -> List[sqlite3.Row]:
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
        requester_user_id: str,
        requester_channel_id: str,
    ) -> int:
        async with self._lock:
            now = _utc_now()
            cursor = self._connection.execute(
                """
                INSERT INTO company_requests (
                    company, normalized_company, careers_url, requester_user_id, requester_channel_id,
                    status, admin_notes, approver_user_id, watchlist_company_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', '', NULL, NULL, ?, ?)
                """,
                (
                    company,
                    normalized_company,
                    careers_url,
                    requester_user_id,
                    requester_channel_id,
                    now,
                    now,
                ),
            )
            self._connection.commit()
            return int(cursor.lastrowid)

    async def find_pending_company_request(
        self,
        *,
        normalized_company: str,
        careers_url: str,
    ) -> Optional[sqlite3.Row]:
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
    ) -> List[sqlite3.Row]:
        sql = "SELECT * FROM company_requests"
        params: List[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._lock:
            return self._connection.execute(sql, params).fetchall()

    async def get_company_request(self, request_id: int) -> Optional[sqlite3.Row]:
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
    ) -> tuple[int, bool]:
        async with self._lock:
            if canonical_id is None:
                now = _utc_now()
                cursor = self._connection.execute(
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
                canonical_id = int(cursor.lastrowid)
                created = True
            else:
                row = self._connection.execute(
                    "SELECT source_names, provenance_tags FROM canonical_jobs WHERE id = ?",
                    (canonical_id,),
                ).fetchone()
                existing_sources = json.loads(row["source_names"]) if row else []
                existing_provenance = json.loads(row["provenance_tags"]) if row else []
                merged_sources = sorted(set(existing_sources + [job.source_name]))
                merged_provenance = sorted(set(existing_provenance + list(job.provenance_tags)))
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
            return canonical_id, created

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
            canonical_count = self._connection.execute("SELECT COUNT(*) FROM canonical_jobs").fetchone()[0]
            variant_count = self._connection.execute("SELECT COUNT(*) FROM job_variants").fetchone()[0]
            posted_count = self._connection.execute(
                "SELECT COUNT(DISTINCT canonical_id) FROM discord_posts"
            ).fetchone()[0]
            dedupe_count = self._connection.execute("SELECT COUNT(*) FROM dedupe_matches").fetchone()[0]
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

    def _row_to_canonical(self, row: Optional[sqlite3.Row]) -> CanonicalJob:
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
