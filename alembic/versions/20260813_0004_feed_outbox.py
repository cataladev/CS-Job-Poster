"""add feed observation identity and Discord delivery outbox

Revision ID: 20260813_0004
Revises: 20260412_0003
Create Date: 2026-08-13 02:00:00
"""

from __future__ import annotations

from alembic import op


revision = "20260813_0004"
down_revision = "20260412_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = [
        """
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
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS feed_observations (
            source_id TEXT NOT NULL,
            external_id TEXT NOT NULL,
            canonical_id BIGINT NOT NULL REFERENCES canonical_jobs(id) ON DELETE CASCADE,
            source_label TEXT NOT NULL,
            source_url TEXT NOT NULL,
            source_ref TEXT NOT NULL DEFAULT '',
            posted_date TEXT,
            payload TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            PRIMARY KEY(source_id, external_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS feed_canonical_keys (
            canonical_key TEXT PRIMARY KEY,
            canonical_id BIGINT NOT NULL REFERENCES canonical_jobs(id) ON DELETE CASCADE,
            key_type TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS discord_deliveries (
            id BIGSERIAL PRIMARY KEY,
            canonical_id BIGINT NOT NULL REFERENCES canonical_jobs(id) ON DELETE CASCADE,
            channel_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN ('suppressed', 'pending', 'sending', 'uncertain', 'sent')
            ),
            marker TEXT NOT NULL UNIQUE,
            message_id TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            sent_at TEXT,
            UNIQUE(canonical_id, channel_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_feed_observations_canonical
            ON feed_observations(canonical_id, source_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_feed_canonical_match
            ON canonical_jobs(track, normalized_company, season_term)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_feed_keys_canonical
            ON feed_canonical_keys(canonical_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_discord_deliveries_state
            ON discord_deliveries(channel_id, state, created_at)
        """,
        """
        INSERT INTO discord_deliveries (
            canonical_id, channel_id, state, marker, message_id, attempts,
            last_error, created_at, updated_at, sent_at
        )
        SELECT
            canonical_id,
            channel_id,
            'sent',
            'jobbot:' || substr(md5(canonical_id::text || ':' || channel_id), 1, 20),
            message_id,
            1,
            NULL,
            posted_at,
            posted_at,
            posted_at
        FROM discord_posts
        ON CONFLICT DO NOTHING
        """,
    ]
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS discord_deliveries")
    op.execute("DROP TABLE IF EXISTS feed_canonical_keys")
    op.execute("DROP TABLE IF EXISTS feed_observations")
    op.execute("DROP INDEX IF EXISTS idx_feed_canonical_match")
    op.execute("DROP TABLE IF EXISTS app_state")
    op.execute("DROP TABLE IF EXISTS feed_state")
