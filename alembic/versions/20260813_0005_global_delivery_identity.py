"""enforce one Discord delivery for each canonical role and location

Revision ID: 20260813_0005
Revises: 20260813_0004
Create Date: 2026-08-13 19:45:00
"""

from __future__ import annotations

from alembic import op


revision = "20260813_0005"
down_revision = "20260813_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY canonical_id
                    ORDER BY
                        CASE state
                            WHEN 'sent' THEN 5
                            WHEN 'uncertain' THEN 4
                            WHEN 'sending' THEN 3
                            WHEN 'pending' THEN 2
                            ELSE 1
                        END DESC,
                        created_at ASC,
                        id ASC
                ) AS row_number
            FROM discord_deliveries
        )
        DELETE FROM discord_deliveries
        WHERE id IN (SELECT id FROM ranked WHERE row_number > 1)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_discord_deliveries_canonical
            ON discord_deliveries(canonical_id)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_discord_deliveries_canonical")
