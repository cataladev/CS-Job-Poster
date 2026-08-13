from __future__ import annotations

import os

import psycopg
import pytest
from alembic import command
from alembic.config import Config


TEST_DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://jobbot:jobbot@localhost:5432/jobbot",
)


def _postgres_available() -> bool:
    try:
        with psycopg.connect(TEST_DATABASE_URL, connect_timeout=2):
            return True
    except Exception:
        return False


def test_feed_outbox_migration_preserves_and_seeds_existing_posts(monkeypatch) -> None:
    if not _postgres_available():
        pytest.skip("Postgres is not available for migration testing.")

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cursor.execute("CREATE SCHEMA public")
            cursor.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")

    alembic_config = Config("alembic.ini")
    command.upgrade(alembic_config, "20260412_0003")

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO canonical_jobs (
                    track, title, company, location, summary, canonical_url,
                    season_term, employment_type, remote_status, status,
                    normalized_company, normalized_title, normalized_locations,
                    dedupe_fingerprint, source_names, provenance_tags, first_seen, last_seen
                ) VALUES (
                    'internship', 'Software Engineer Intern', 'Example Corp', 'Austin, TX',
                    '', 'https://jobs.example.com/123', 'Summer 2027', NULL, 'unknown',
                    'active', 'example', 'software engineer intern', '["austin tx"]',
                    'legacy-fingerprint', '["legacy"]', '[]',
                    '2026-08-12T00:00:00+00:00', '2026-08-12T00:00:00+00:00'
                )
                RETURNING id
                """
            )
            canonical_id = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO discord_posts (
                    canonical_id, track, channel_id, message_id, posted_at
                ) VALUES (%s, 'internship', '111', '456', '2026-08-12T00:00:00+00:00')
                """,
                (canonical_id,),
            )
            cursor.execute(
                """
                INSERT INTO discord_posts (
                    canonical_id, track, channel_id, message_id, posted_at
                ) VALUES (%s, 'internship', '222', '789', '2026-08-12T01:00:00+00:00')
                """,
                (canonical_id,),
            )
        connection.commit()

    command.upgrade(alembic_config, "head")

    with psycopg.connect(TEST_DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT canonical_id, channel_id, state, marker, message_id
                FROM discord_deliveries
                """
            )
            deliveries = cursor.fetchall()
            assert len(deliveries) == 1
            delivery = deliveries[0]
            assert delivery is not None
            assert delivery[0] == canonical_id
            assert delivery[1:] == (
                "111",
                "sent",
                delivery[3],
                "456",
            )
            assert delivery[3].startswith("jobbot:")

            cursor.execute("SELECT title FROM canonical_jobs WHERE id = %s", (canonical_id,))
            assert cursor.fetchone() == ("Software Engineer Intern",)
