"""initial postgres schema

Revision ID: 20260409_0001
Revises:
Create Date: 2026-04-09 16:30:00
"""

from __future__ import annotations

from alembic import op


revision = "20260409_0001"
down_revision = None
branch_labels = None
depends_on = None


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS canonical_jobs (
        id BIGSERIAL PRIMARY KEY,
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
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_variants (
        id BIGSERIAL PRIMARY KEY,
        canonical_id BIGINT NOT NULL REFERENCES canonical_jobs(id) ON DELETE CASCADE,
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
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fingerprints (
        fingerprint_key TEXT PRIMARY KEY,
        canonical_id BIGINT NOT NULL REFERENCES canonical_jobs(id) ON DELETE CASCADE,
        fingerprint_type TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS discord_posts (
        id BIGSERIAL PRIMARY KEY,
        canonical_id BIGINT NOT NULL REFERENCES canonical_jobs(id) ON DELETE CASCADE,
        track TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        posted_at TEXT NOT NULL,
        UNIQUE(canonical_id, channel_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_runs (
        id BIGSERIAL PRIMARY KEY,
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
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_errors (
        id BIGSERIAL PRIMARY KEY,
        source_name TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        error TEXT NOT NULL,
        context TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS github_repo_state (
        repository TEXT NOT NULL,
        path TEXT NOT NULL,
        etag TEXT,
        content_sha TEXT,
        last_modified TEXT,
        last_checked_at TEXT NOT NULL,
        PRIMARY KEY(repository, path)
    )
    """,
    """
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
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dedupe_matches (
        id BIGSERIAL PRIMARY KEY,
        canonical_id BIGINT NOT NULL REFERENCES canonical_jobs(id) ON DELETE CASCADE,
        variant_key TEXT NOT NULL,
        reason TEXT NOT NULL,
        score REAL,
        matched_on TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS company_watchlist (
        id BIGSERIAL PRIMARY KEY,
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
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS company_requests (
        id BIGSERIAL PRIMARY KEY,
        company TEXT NOT NULL,
        normalized_company TEXT NOT NULL,
        careers_url TEXT NOT NULL,
        homepage_url TEXT NOT NULL DEFAULT '',
        requester_user_id TEXT NOT NULL,
        requester_channel_id TEXT NOT NULL,
        status TEXT NOT NULL,
        admin_notes TEXT NOT NULL,
        approver_user_id TEXT,
        watchlist_company_id BIGINT REFERENCES company_watchlist(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS company_scan_targets (
        id BIGSERIAL PRIMARY KEY,
        company_id BIGINT NOT NULL REFERENCES company_watchlist(id) ON DELETE CASCADE,
        target_type TEXT NOT NULL,
        locator TEXT NOT NULL,
        confidence REAL NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        verified_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(company_id, target_type, locator)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS company_scan_history (
        id BIGSERIAL PRIMARY KEY,
        company_id BIGINT NOT NULL REFERENCES company_watchlist(id) ON DELETE CASCADE,
        target_type TEXT NOT NULL,
        track TEXT NOT NULL,
        scanned_at TEXT NOT NULL,
        success INTEGER NOT NULL,
        jobs_found INTEGER NOT NULL,
        error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_user_profiles (
        user_id TEXT PRIMARY KEY,
        visibility TEXT NOT NULL DEFAULT 'matched',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_application_statuses (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES job_user_profiles(user_id) ON DELETE CASCADE,
        company_id BIGINT NOT NULL REFERENCES company_watchlist(id) ON DELETE CASCADE,
        status TEXT NOT NULL,
        notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(user_id, company_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS process_notes (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES job_user_profiles(user_id) ON DELETE CASCADE,
        company_id BIGINT NOT NULL REFERENCES company_watchlist(id) ON DELETE CASCADE,
        body TEXT NOT NULL,
        legacy_status_id BIGINT UNIQUE REFERENCES job_application_statuses(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_process_notes_company_created_at
        ON process_notes(company_id, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_process_notes_user_company
        ON process_notes(user_id, company_id)
    """,
]

DROP_STATEMENTS = [
    "DROP INDEX IF EXISTS idx_process_notes_user_company",
    "DROP INDEX IF EXISTS idx_process_notes_company_created_at",
    "DROP TABLE IF EXISTS process_notes CASCADE",
    "DROP TABLE IF EXISTS job_application_statuses CASCADE",
    "DROP TABLE IF EXISTS job_user_profiles CASCADE",
    "DROP TABLE IF EXISTS company_scan_history CASCADE",
    "DROP TABLE IF EXISTS company_scan_targets CASCADE",
    "DROP TABLE IF EXISTS company_requests CASCADE",
    "DROP TABLE IF EXISTS company_watchlist CASCADE",
    "DROP TABLE IF EXISTS dedupe_matches CASCADE",
    "DROP TABLE IF EXISTS board_seeds CASCADE",
    "DROP TABLE IF EXISTS github_repo_state CASCADE",
    "DROP TABLE IF EXISTS source_errors CASCADE",
    "DROP TABLE IF EXISTS source_runs CASCADE",
    "DROP TABLE IF EXISTS discord_posts CASCADE",
    "DROP TABLE IF EXISTS fingerprints CASCADE",
    "DROP TABLE IF EXISTS job_variants CASCADE",
    "DROP TABLE IF EXISTS canonical_jobs CASCADE",
]


def upgrade() -> None:
    for statement in SCHEMA_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in DROP_STATEMENTS:
        op.execute(statement)
