"""add command receipt table

Revision ID: 20260412_0003
Revises: 20260410_0002
Create Date: 2026-04-12 18:05:00
"""

from __future__ import annotations

from alembic import op


revision = "20260412_0003"
down_revision = "20260410_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS discord_command_receipts (
            message_id TEXT PRIMARY KEY,
            command_name TEXT NOT NULL,
            author_user_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            guild_id TEXT,
            claimed_at TEXT NOT NULL,
            instance_id TEXT NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS discord_command_receipts")
