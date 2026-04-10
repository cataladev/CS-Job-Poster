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
        async def get_watchlist_company_by_normalized_company(self, normalized_company):
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


@pytest.mark.asyncio
async def test_resolve_logo_url_prefers_cached_verified_logo(config_factory) -> None:
    class DummyDatabase:
        async def get_watchlist_company_by_normalized_company(self, normalized_company):
            return {
                "id": 1,
                "homepage_url": "https://openai.com",
                "careers_url": "https://jobs.ashbyhq.com/openai",
                "logo_url": "https://cdn.example.com/openai-logo.png",
                "logo_source": "ashby",
                "logo_verified_at": "2026-04-08T12:00:00+00:00",
            }

        async def update_watchlist_branding(self, **kwargs):
            return False

    publisher = DiscordPublisher(config_factory(), database=DummyDatabase())  # type: ignore[arg-type]
    job = _canonical_job(
        job_id=7,
        title="Software Engineer Intern",
        normalized_title="software engineer intern",
        canonical_url="https://jobs.ashbyhq.com/openai/7",
    )
    job.company = "OpenAI"
    job.normalized_company = "openai"

    logo_url = await publisher._resolve_logo_url(job)

    assert logo_url == "https://cdn.example.com/openai-logo.png"


@pytest.mark.asyncio
async def test_refresh_jobs_uses_resolved_logo_on_existing_post(config_factory) -> None:
    class DummyMessage:
        def __init__(self):
            self.embed = None

        async def edit(self, *, embed):
            self.embed = embed

    class DummyChannel:
        def __init__(self, message):
            self.message = message

        async def fetch_message(self, message_id):
            return self.message

    class DummyBot:
        def __init__(self, channel):
            self.channel = channel

        async def wait_until_ready(self):
            return None

        def get_channel(self, channel_id):
            return self.channel

        async def fetch_channel(self, channel_id):
            return self.channel

    class DummyDatabase:
        async def get_post_records_for_canonical(self, canonical_id):
            return [{"channel_id": "111", "message_id": "222"}]

        async def get_watchlist_company_by_normalized_company(self, normalized_company):
            return {
                "id": 1,
                "homepage_url": "https://openai.com",
                "careers_url": "https://jobs.ashbyhq.com/openai",
                "logo_url": "https://cdn.example.com/openai-logo.png",
                "logo_source": "ashby",
                "logo_verified_at": "2026-04-08T12:00:00+00:00",
            }

        async def update_watchlist_branding(self, **kwargs):
            return False

    message = DummyMessage()
    publisher = DiscordPublisher(config_factory(), database=DummyDatabase())  # type: ignore[arg-type]
    publisher.bind_bot(DummyBot(DummyChannel(message)))  # type: ignore[arg-type]

    job = _canonical_job(
        job_id=8,
        title="Software Engineer Intern",
        normalized_title="software engineer intern",
        canonical_url="https://jobs.ashbyhq.com/openai/8",
    )
    job.company = "OpenAI"
    job.normalized_company = "openai"

    refreshed = await publisher.refresh_jobs([job])

    assert refreshed == [8]
    assert message.embed.thumbnail.url == "https://cdn.example.com/openai-logo.png"


@pytest.mark.asyncio
async def test_publish_jobs_uses_updated_runtime_channel_without_restart(config_factory) -> None:
    class DummyMessage:
        id = 999

    class DummyChannel:
        def __init__(self, channel_id):
            self.id = channel_id
            self.sent_embeds = []

        async def send(self, *, embed):
            self.sent_embeds.append(embed)
            return DummyMessage()

    class DummyBot:
        def __init__(self, channels):
            self.channels = channels

        async def wait_until_ready(self):
            return None

        def get_channel(self, channel_id):
            return self.channels.get(channel_id)

        async def fetch_channel(self, channel_id):
            return self.channels[channel_id]

    class DummyDatabase:
        def __init__(self):
            self.marked_posts = []

        async def is_posted(self, canonical_id, channel_id):
            return False

        async def get_posted_candidates(self, **kwargs):
            return []

        async def mark_posted(self, canonical_id, track, channel_id, message_id):
            self.marked_posts.append((canonical_id, track, channel_id, message_id))

    config = config_factory()
    config.discord_channel_id = 333
    database = DummyDatabase()
    internship_channel = DummyChannel(333)
    publisher = DiscordPublisher(config, database=database)  # type: ignore[arg-type]
    publisher.bind_bot(DummyBot({333: internship_channel}))  # type: ignore[arg-type]

    async def fake_resolve_logo_url(*args, **kwargs):
        return None

    publisher._resolve_logo_url = fake_resolve_logo_url  # type: ignore[assignment]

    job = _canonical_job(
        job_id=9,
        title="Software Engineer Intern",
        normalized_title="software engineer intern",
        canonical_url="https://example.com/jobs/9",
    )

    posted = await publisher.publish_jobs([job])

    assert posted == [9]
    assert len(internship_channel.sent_embeds) == 1
    assert database.marked_posts == [(9, "internship", 333, 999)]


@pytest.mark.asyncio
async def test_publish_jobs_skips_new_grad_when_channel_disabled(config_factory) -> None:
    class DummyBot:
        async def wait_until_ready(self):
            return None

    class DummyDatabase:
        async def is_posted(self, canonical_id, channel_id):
            return False

        async def get_posted_candidates(self, **kwargs):
            return []

        async def mark_posted(self, canonical_id, track, channel_id, message_id):
            raise AssertionError("mark_posted should not be called when the channel is disabled")

    config = config_factory()
    config.discord_new_grad_channel_id = None
    publisher = DiscordPublisher(config, database=DummyDatabase())  # type: ignore[arg-type]
    publisher.bind_bot(DummyBot())  # type: ignore[arg-type]

    job = _canonical_job(
        job_id=10,
        title="Software Engineer, New Grad",
        normalized_title="software engineer new grad",
        canonical_url="https://example.com/jobs/10",
    )
    job.track = "new_grad"

    posted = await publisher.publish_jobs([job])

    assert posted == []
