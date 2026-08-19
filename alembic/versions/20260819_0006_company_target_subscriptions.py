"""add persistent company target subscriptions

Revision ID: 20260819_0006
Revises: 20260813_0005
Create Date: 2026-08-19 12:00:00
"""

from __future__ import annotations

from alembic import op


revision = "20260819_0006"
down_revision = "20260813_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS company_target_subscriptions (
            guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            track TEXT NOT NULL CHECK (track IN ('internship', 'new_grad')),
            company TEXT NOT NULL,
            company_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id, track, company_key)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_company_target_subscriptions_lookup
            ON company_target_subscriptions(guild_id, track, company_key, created_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_company_target_subscriptions_lookup")
    op.execute("DROP TABLE IF EXISTS company_target_subscriptions")
