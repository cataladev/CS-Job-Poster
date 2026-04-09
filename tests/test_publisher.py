from datetime import datetime, timezone

import pytest

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


def test_compact_summary_prefers_role_specific_sentence(config_factory) -> None:
    publisher = DiscordPublisher(config_factory(), database=None)  # type: ignore[arg-type]
    summary = (
        "Tenstorrent is leading the industry on cutting-edge AI technology, revolutionizing "
        "performance expectations and ease of use. Build backend platform services for the "
        "intern engineering team."
    )

    assert publisher._compact_summary(summary) == "Build backend platform services for the intern engineering team."


def test_compact_summary_falls_back_when_only_boilerplate_exists(config_factory) -> None:
    publisher = DiscordPublisher(config_factory(), database=None)  # type: ignore[arg-type]
    summary = (
        "Tenstorrent is leading the industry on cutting-edge AI technology, revolutionizing "
        "performance expectations, ease of use, and cost efficiency."
    )

    assert publisher._compact_summary(summary) == "Open listing for details."


def test_build_logo_thumbnail_url_uses_company_homepage(config_factory) -> None:
    publisher = DiscordPublisher(config_factory(), database=None)  # type: ignore[arg-type]

    url = publisher._build_logo_thumbnail_url(
        homepage_url="https://openai.com/company",
        careers_url="https://jobs.ashbyhq.com/openai",
        canonical_url="https://jobs.ashbyhq.com/openai/123",
    )

    assert url == "https://icons.duckduckgo.com/ip3/openai.com.ico"


def test_build_logo_thumbnail_url_skips_shared_ats_hosts(config_factory) -> None:
    publisher = DiscordPublisher(config_factory(), database=None)  # type: ignore[arg-type]

    url = publisher._build_logo_thumbnail_url(
        homepage_url="https://jobs.lever.co",
        careers_url="https://jobs.lever.co/example",
        canonical_url="https://jobs.lever.co/example/123",
    )

    assert url is None


def test_build_embed_sets_thumbnail_when_logo_url_present(config_factory) -> None:
    publisher = DiscordPublisher(config_factory(), database=None)  # type: ignore[arg-type]
    job = _canonical_job(
        job_id=3,
        title="Software Engineer Intern",
        normalized_title="software engineer intern",
        canonical_url="https://example.com/jobs/3",
    )

    embed = publisher._build_embed(
        job,
        logo_url="https://icons.duckduckgo.com/ip3/openai.com.ico",
    )

    assert embed.thumbnail.url == "https://icons.duckduckgo.com/ip3/openai.com.ico"


def test_build_embed_uses_troll_emoji_when_logo_url_missing(config_factory) -> None:
    publisher = DiscordPublisher(config_factory(), database=None)  # type: ignore[arg-type]
    job = _canonical_job(
        job_id=4,
        title="Software Engineer Intern",
        normalized_title="software engineer intern",
        canonical_url="https://example.com/jobs/4",
    )

    embed = publisher._build_embed(job, logo_url=None)

    company_field = next(field for field in embed.fields if field.name == "Company")
    assert company_field.value == "🧌 Stripe"
    assert embed.thumbnail.url is None


def test_build_embed_uses_human_readable_source_labels(config_factory) -> None:
    publisher = DiscordPublisher(config_factory(), database=None)  # type: ignore[arg-type]
    job = _canonical_job(
        job_id=6,
        title="Software Engineer Intern",
        normalized_title="software engineer intern",
        canonical_url="https://example.com/jobs/6",
    )
    job.source_names = ["greenhouse", "github_simplify_internships", "company_watchlist_internship"]

    embed = publisher._build_embed(job, logo_url=None)

    source_field = next(field for field in embed.fields if field.name == "Source(s)")
    assert source_field.value == "GitHub, Company Page, Greenhouse"


@pytest.mark.asyncio
async def test_resolve_logo_url_falls_back_to_troll_thumbnail(config_factory) -> None:
    class DummyDatabase:
        async def get_company_branding(self, normalized_company):
            return None

    publisher = DiscordPublisher(config_factory(), database=DummyDatabase())  # type: ignore[arg-type]
    job = _canonical_job(
        job_id=5,
        title="Software Engineer Intern",
        normalized_title="software engineer intern",
        canonical_url="https://jobs.lever.co/example/5",
    )

    logo_url = await publisher._resolve_logo_url(job)

    assert logo_url == "https://cdnjs.cloudflare.com/ajax/libs/twemoji/14.0.2/72x72/1f9cc.png"
