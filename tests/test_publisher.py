from datetime import datetime, timezone

from job_bot.models import CanonicalJob
from job_bot.publisher import DiscordPublisher


def _canonical_job(
    *,
    job_id: int,
    title: str,
    normalized_title: str,
    canonical_url: str,
    location: str = "New York, NY",
    normalized_locations=None,
    season_term: str = "summer 2026",
) -> CanonicalJob:
    return CanonicalJob(
        id=job_id,
        track="internship",
        title=title,
        company="Stripe",
        location=location,
        canonical_url=canonical_url,
        summary="Software internship",
        season_term=season_term,
        employment_type=None,
        remote_status="onsite",
        status="active",
        normalized_company="stripe",
        normalized_title=normalized_title,
        normalized_locations=normalized_locations or ["new york ny"],
        dedupe_fingerprint=f"fingerprint-{job_id}",
        source_names=["github_simplify_internships"],
        provenance_tags=[],
        first_seen=datetime.now(timezone.utc),
        last_seen=datetime.now(timezone.utc),
    )


def test_publisher_repeat_guard_flags_near_duplicate(config_factory) -> None:
    publisher = DiscordPublisher(config_factory(), database=None)  # type: ignore[arg-type]
    existing = _canonical_job(
        job_id=1,
        title="Software Engineer Intern",
        normalized_title="software engineer intern",
        canonical_url="https://jobs.example.com/stripe/1",
    )
    candidate = _canonical_job(
        job_id=2,
        title="Software Engineering Intern",
        normalized_title="software engineer intern",
        canonical_url="https://job-boards.greenhouse.io/stripe/jobs/2",
    )

    assert publisher._is_probable_repeat(candidate, existing) is True


def test_publisher_repeat_guard_allows_distinct_roles(config_factory) -> None:
    publisher = DiscordPublisher(config_factory(), database=None)  # type: ignore[arg-type]
    existing = _canonical_job(
        job_id=1,
        title="Backend Software Engineer Intern",
        normalized_title="backend software engineer intern",
        canonical_url="https://jobs.example.com/stripe/backend",
    )
    candidate = _canonical_job(
        job_id=2,
        title="Frontend Software Engineer Intern",
        normalized_title="frontend software engineer intern",
        canonical_url="https://jobs.example.com/stripe/frontend",
    )

    assert publisher._is_probable_repeat(candidate, existing) is False
