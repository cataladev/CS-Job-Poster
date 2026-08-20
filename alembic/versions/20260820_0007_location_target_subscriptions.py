"""add persistent location target subscriptions

Revision ID: 20260820_0007
Revises: 20260819_0006
Create Date: 2026-08-20 12:00:00
"""

from __future__ import annotations

from alembic import op


revision = "20260820_0007"
down_revision = "20260819_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS location_target_subscriptions (
            guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            track TEXT NOT NULL CHECK (track IN ('internship', 'new_grad')),
            location TEXT NOT NULL,
            location_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id, track, location_key)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_location_target_subscriptions_lookup
            ON location_target_subscriptions(guild_id, track, location_key, created_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_location_target_subscriptions_lookup")
    op.execute("DROP TABLE IF EXISTS location_target_subscriptions")
