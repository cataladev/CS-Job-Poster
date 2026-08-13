from __future__ import annotations

import os
from dataclasses import asdict

import psycopg
import pytest

from job_bot.database import Database
from job_bot.github_feeds import JobObservation
from job_bot.models import JobPosting
from job_bot.utils.feed_normalization import role_signature
from job_bot.utils.normalization import normalize_job_posting


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
        assert await database.acquire_primary_runtime_lock() is True

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

        observation = JobObservation(
            source_id="simplify_internships",
            source_label="Simplify",
            source_url="https://github.com/SimplifyJobs/Summer2027-Internships",
            external_id="stripe-123",
            company="Stripe",
            title="Software Engineer Intern",
            locations=["New York, NY"],
            application_url="https://stripe.com/jobs/123",
            track="internship",
        )
        normalized = normalize_job_posting(observation.to_job_posting())
        ingest = await database.ingest_feed_job(
            job=normalized,
            source_id=observation.source_id,
            external_id=observation.external_id,
            source_label=observation.source_label,
            source_url=observation.source_url,
            source_ref=observation.source_ref,
            posted_date=None,
            payload=asdict(observation),
            canonical_keys={
                f"url:{normalized.canonical_url}",
                role_signature(
                    track=normalized.track,
                    company=normalized.normalized_company,
                    title=normalized.normalized_title,
                    cycle=normalized.season_term,
                    normalized_locations=normalized.normalized_locations,
                ),
            },
            channel_id=111,
            suppress_delivery=True,
        )
        assert ingest.canonical_id == canonical_id
        assert ingest.delivery_state == "suppressed"
        assert await database.get_delivery_counts() == {"suppressed": 1}

        stats = await database.get_stats()
        assert stats["total_canonical_jobs"] == 1
        # The legacy row and the feed observation are separate provenance variants,
        # even though deduplication keeps them under one canonical job.
        assert stats["total_jobs_found"] == 2

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

        assert await database.claim_command_receipt(
            message_id="9001",
            command_name="help",
            author_user_id="42",
            channel_id="99",
            guild_id="77",
            instance_id="instance-a",
        )
        assert not await database.claim_command_receipt(
            message_id="9001",
            command_name="help",
            author_user_id="42",
            channel_id="99",
            guild_id="77",
            instance_id="instance-b",
        )

        second_database = Database(TEST_DATABASE_URL)
        await second_database.initialize()
        try:
            assert await second_database.acquire_primary_runtime_lock() is False
        finally:
            await second_database.close()
    finally:
        await database.close()
