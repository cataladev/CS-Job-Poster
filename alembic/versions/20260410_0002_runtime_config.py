"""add runtime config table

Revision ID: 20260410_0002
Revises: 20260409_0001
Create Date: 2026-04-10 00:00:00
"""

from __future__ import annotations

from alembic import op


revision = "20260410_0002"
down_revision = "20260409_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_config (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_by_user_id TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS runtime_config")
