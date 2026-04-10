from __future__ import annotations

import os

import psycopg
import pytest

from job_bot.database import Database
from job_bot.models import JobPosting


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


def _reset_public_schema() -> None:
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cursor.execute("CREATE SCHEMA public")
            cursor.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")


@pytest.mark.asyncio
async def test_postgres_backend_can_initialize_and_persist() -> None:
    if not _postgres_available():
        pytest.skip("Postgres is not available for integration testing.")

    _reset_public_schema()
    database = Database(TEST_DATABASE_URL)
    await database.initialize()

    try:
        assert database.backend == "postgres"

        company_id = await database.upsert_watchlist_company(
            company="Stripe",
            normalized_company="stripe",
            careers_url="https://stripe.com/jobs",
            homepage_url="https://stripe.com",
            country="US",
            categories=["big_tech"],
            tier="t1",
            scan_cadence="every_run",
            tracks=["both"],
            seed_source="test",
            notes="",
            active=True,
            next_scan_at=None,
        )
        assert company_id > 0

        canonical_id, created, refresh_required = await database.save_job(
            JobPosting(
                title="Software Engineer Intern",
                company="Stripe",
                locations=["New York, NY"],
                source_name="github_simplify_internships",
                track="internship",
                url="https://stripe.com/jobs/123",
                canonical_url="https://stripe.com/jobs/123",
                summary="Summer internship",
                normalized_company="stripe",
                normalized_title="software engineer intern",
                normalized_locations=["new york ny"],
                canonical_location="new york ny",
                dedupe_fingerprint="stripe|software engineer intern|summer 2026|new york ny",
                raw_data={"age": "0d"},
            )
        )

        assert canonical_id > 0
        assert created is True
        assert refresh_required is False

        stats = await database.get_stats()
        assert stats["total_canonical_jobs"] == 1
        assert stats["total_jobs_found"] == 1

        runtime_row = await database.set_runtime_config_value(
            "discord_channel_id",
            "333",
            "42",
        )
        assert runtime_row["value"] == "333"

        disabled_new_grad = await database.set_runtime_config_value(
            "discord_new_grad_channel_id",
            None,
            "42",
        )
        assert disabled_new_grad["value"] is None

        runtime_rows = await database.list_runtime_config()
        assert {row["key"]: row["value"] for row in runtime_rows} == {
            "discord_channel_id": "333",
            "discord_new_grad_channel_id": None,
        }
    finally:
        await database.close()
