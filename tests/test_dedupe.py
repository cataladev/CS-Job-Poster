import pytest

from job_bot.database import Database
from job_bot.dedupe import DedupeEngine
from job_bot.models import JobPosting
from job_bot.utils.normalization import normalize_job_posting, normalize_location


def test_normalize_location_matches_greenhouse_country_verbose_format() -> None:
    assert normalize_location("Seattle, Washington, United States") == "seattle wa"


def test_normalize_location_matches_country_first_format() -> None:
    assert normalize_location("United States, Washington, Redmond") == "redmond wa"
    assert (
        normalize_location("United States, California, Mountain View")
        == "mountain view ca"
    )


@pytest.mark.asyncio
async def test_cross_platform_duplicate_detected(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    dedupe = DedupeEngine(database)

    github_job = normalize_job_posting(
        JobPosting(
            title="Software Engineer Intern",
            company="Stripe, Inc.",
            locations=["NYC"],
            source_name="github",
            track="internship",
            url="https://linkedin.example/jobs/1234",
            summary="Summer 2026 internship",
        )
    )
    canonical_id, _, _ = await database.save_job(github_job)

    greenhouse_job = normalize_job_posting(
        JobPosting(
            title="Software Engineering Intern",
            company="Stripe",
            locations=["New York, NY"],
            source_name="greenhouse",
            track="internship",
            url="https://job-boards.greenhouse.io/stripe/jobs/555",
            summary="Summer 2026 internship",
        )
    )
    decision = await dedupe.resolve(greenhouse_job)

    assert decision.is_duplicate is True
    assert decision.canonical_id == canonical_id


@pytest.mark.asyncio
async def test_distinct_openings_are_not_merged(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    dedupe = DedupeEngine(database)

    backend = normalize_job_posting(
        JobPosting(
            title="Backend Software Engineer Intern",
            company="Stripe",
            locations=["New York, NY"],
            source_name="github",
            track="internship",
            url="https://example.com/jobs/backend",
            summary="Fall 2026 internship",
        )
    )
    await database.save_job(backend)

    frontend = normalize_job_posting(
        JobPosting(
            title="Frontend Software Engineer Intern",
            company="Stripe",
            locations=["New York, NY"],
            source_name="greenhouse",
            track="internship",
            url="https://example.com/jobs/frontend",
            summary="Fall 2026 internship",
        )
    )
    decision = await dedupe.resolve(frontend)

    assert decision.is_duplicate is False
