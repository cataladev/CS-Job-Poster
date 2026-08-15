from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
import pytest

from job_bot.database import Database
from job_bot.feed_publisher import TrackChannelPublisher
from job_bot.github_feeds import JobObservation
from job_bot.utils.feed_normalization import role_signature, sanitize_application_url
from job_bot.utils.normalization import normalize_job_posting


class FakeChannel:
    def __init__(self, messages, *, channel_id=111, permissions=None, send_error=None):
        self.messages = messages
        self.sent = []
        self.contents = []
        self.allowed_mentions = []
        self.id = channel_id
        self.guild = SimpleNamespace(id=123, me=object())
        self.type = discord.ChannelType.text
        self.permissions = permissions or SimpleNamespace(
            view_channel=True,
            send_messages=True,
            embed_links=True,
            read_message_history=True,
            mention_everyone=True,
        )
        self.send_error = send_error

    def permissions_for(self, _member):
        return self.permissions

    async def send(self, *, embed, content=None, allowed_mentions=None):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(embed)
        self.contents.append(content)
        self.allowed_mentions.append(allowed_mentions)
        return SimpleNamespace(id=999 + len(self.sent))

    def history(self, **_kwargs):
        async def generate():
            for message in self.messages:
                yield message

        return generate()


class FakeClient:
    def __init__(self, channel):
        self.user = SimpleNamespace(id=42)
        self.channels = channel if isinstance(channel, dict) else {111: channel}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        return self.channels.get(channel_id)


def test_daily_digest_is_scheduled_for_four_pm_new_york_time(config_factory) -> None:
    publisher = TrackChannelPublisher(config_factory(), Database(":memory:"))

    summer = publisher._daily_digest_at(datetime(2026, 8, 15, 18, tzinfo=UTC))
    winter = publisher._daily_digest_at(datetime(2026, 12, 15, 18, tzinfo=UTC))

    assert summer == datetime(2026, 8, 15, 20, tzinfo=UTC)
    assert winter == datetime(2026, 12, 15, 21, tzinfo=UTC)


async def _create_pending_delivery(
    database: Database,
    *,
    track: str = "internship",
    channel_id: int = 111,
    external_id: str = "job-1",
    location: str = "Austin, TX",
    company: str = "Example Corp",
    raw_data: dict | None = None,
) -> None:
    title = "Software Engineer Intern" if track == "internship" else "Software Engineer"
    observation = JobObservation(
        source_id="source",
        source_label="Source",
        source_url="https://github.com/owner/source",
        external_id=external_id,
        company=company,
        title=title,
        locations=[location],
        application_url=sanitize_application_url(
            f"https://jobs.example.com/{external_id}"
        ).url,
        track=track,
        cycle="Summer 2027",
        raw_data=raw_data or {},
    )
    job = normalize_job_posting(observation.to_job_posting())
    key = role_signature(
        track=job.track,
        company=job.normalized_company,
        title=job.normalized_title,
        cycle=job.season_term,
        normalized_locations=job.normalized_locations,
    )
    await database.ingest_feed_job(
        job=job,
        source_id=observation.source_id,
        external_id=observation.external_id,
        source_label=observation.source_label,
        source_url=observation.source_url,
        source_ref=observation.source_ref,
        posted_date=None,
        payload=asdict(observation),
        canonical_keys={f"url:{job.canonical_url}", key},
        channel_id=channel_id,
        suppress_delivery=False,
    )


@pytest.mark.asyncio
async def test_crash_window_reconciles_marker_without_second_send(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        await _create_pending_delivery(database)
        delivery = (await database.list_deliveries_for_send(111))[0]
        await database.mark_delivery_sending(int(delivery["id"]))

        embed = discord.Embed(title="Already delivered")
        embed.set_footer(text=f"Job Bot • {delivery['marker']}")
        existing_message = SimpleNamespace(
            id=777,
            author=SimpleNamespace(id=42),
            embeds=[embed],
        )
        channel = FakeChannel([existing_message])
        publisher = TrackChannelPublisher(config, database)
        publisher.bind_client(FakeClient(channel))  # type: ignore[arg-type]

        sent, reconciled = await publisher.publish_pending()

        assert sent == 0
        assert reconciled == 1
        assert channel.sent == []
        assert await database.get_delivery_counts() == {"sent": 1}
        assert (await database.get_stats())["total_posted"] == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_tracks_publish_to_their_separate_configured_channels(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        await _create_pending_delivery(database, track="internship", channel_id=111)
        await _create_pending_delivery(
            database,
            track="new_grad",
            channel_id=222,
            external_id="new-grad-1",
        )
        internship_channel = FakeChannel([])
        new_grad_channel = FakeChannel([], channel_id=222)
        publisher = TrackChannelPublisher(config, database)
        publisher.bind_client(  # type: ignore[arg-type]
            FakeClient({111: internship_channel, 222: new_grad_channel})
        )

        sent, reconciled = await publisher.publish_pending()

        assert (sent, reconciled) == (2, 0)
        assert len(internship_channel.sent) == 1
        assert len(new_grad_channel.sent) == 1
        assert next(
            field.value
            for field in internship_channel.sent[0].fields
            if field.name == "Track"
        ) == "Internship"
        assert next(
            field.value
            for field in new_grad_channel.sent[0].fields
            if field.name == "Track"
        ) == "New Grad"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_publisher_suppresses_non_us_pending_delivery(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        await _create_pending_delivery(database, location="Toronto, ON, Canada")
        channel = FakeChannel([])
        publisher = TrackChannelPublisher(config, database)
        publisher.bind_client(FakeClient(channel))  # type: ignore[arg-type]

        assert await publisher.publish_pending() == (0, 0)
        assert channel.sent == []
        assert await database.get_delivery_counts() == {"suppressed": 1}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_missing_history_permission_fails_closed(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        await _create_pending_delivery(database)
        permissions = SimpleNamespace(
            view_channel=True,
            send_messages=True,
            embed_links=True,
            read_message_history=False,
        )
        channel = FakeChannel([], permissions=permissions)
        publisher = TrackChannelPublisher(config, database)
        publisher.bind_client(FakeClient(channel))  # type: ignore[arg-type]

        assert await publisher.publish_pending() == (0, 0)

        assert channel.sent == []
        assert await database.get_delivery_counts() == {"pending": 1}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_ambiguous_send_stays_uncertain_until_marker_reconciliation(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        await _create_pending_delivery(database)
        channel = FakeChannel([], send_error=asyncio.TimeoutError("unknown outcome"))
        publisher = TrackChannelPublisher(config, database)
        publisher.bind_client(FakeClient(channel))  # type: ignore[arg-type]

        sent, reconciled = await publisher.publish_pending()
        assert (sent, reconciled) == (0, 0)
        assert await database.get_delivery_counts() == {"uncertain": 1}

        delivery = (await database.list_deliveries_for_send(111))[0]
        embed = discord.Embed(title="Accepted during timeout")
        embed.set_footer(text=f"Job Bot • {delivery['marker']}")
        channel.messages.append(
            SimpleNamespace(id=888, author=SimpleNamespace(id=42), embeds=[embed])
        )
        channel.send_error = None

        sent, reconciled = await publisher.publish_pending()
        assert (sent, reconciled) == (0, 1)
        assert channel.sent == []
        assert await database.get_delivery_counts() == {"sent": 1}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_embed_omits_feed_credit_and_contains_safe_apply_link(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        await _create_pending_delivery(database)
        delivery = (await database.list_deliveries_for_send(111))[0]
        job = (await database.get_canonical_jobs([int(delivery["canonical_id"])]))[0]
        metadata = await database.get_feed_observation_metadata(int(delivery["canonical_id"]))
        metadata["posted_date"] = "2026-08-13"
        publisher = TrackChannelPublisher(config, database)

        embed = publisher.build_embed(job, marker=delivery["marker"], metadata=metadata)

        assert embed.url == "https://jobs.example.com/job-1"
        assert embed.description == "[Apply here](https://jobs.example.com/job-1)"
        assert {field.name for field in embed.fields} == {
            "Company",
            "Location",
            "Track",
            "Cycle",
            "Posted",
        }
        assert all(field.name != "Source(s)" for field in embed.fields)
        assert "github.com/owner/source" not in str(embed.to_dict())
        assert delivery["marker"] in embed.footer.text
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_delivery_uses_source_company_logo_thumbnail(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        logo_url = "https://cdn.example.com/company-logo.png?token=still-valid"
        await _create_pending_delivery(
            database,
            raw_data={"company_logo_url": logo_url},
        )
        channel = FakeChannel([])
        publisher = TrackChannelPublisher(config, database)
        publisher.bind_client(FakeClient(channel))  # type: ignore[arg-type]

        sent, reconciled = await publisher.publish_pending()

        assert (sent, reconciled) == (1, 0)
        assert channel.sent[0].thumbnail.url == logo_url
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_periodic_role_digest_links_back_to_individual_job_messages(config_factory) -> None:
    config = config_factory()
    config.discord_internship_alert_role_id = 555
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        await _create_pending_delivery(
            database,
            external_id="job-1",
            location="Austin, TX",
            company="Amazon",
        )
        await _create_pending_delivery(database, external_id="job-2", location="Boston, MA")
        channel = FakeChannel([])
        publisher = TrackChannelPublisher(config, database)
        publisher._priority_companies = {"amazon"}
        local_now = datetime.now(ZoneInfo("America/New_York")).replace(
            hour=16, minute=5, second=0, microsecond=0
        )
        digest_now = local_now.astimezone(UTC)
        publisher._now = lambda: digest_now  # type: ignore[method-assign]
        publisher.bind_client(FakeClient(channel))  # type: ignore[arg-type]

        sent, reconciled = await publisher.publish_pending()
        assert (sent, reconciled) == (2, 0)
        assert len(channel.sent) == 2
        assert channel.contents == [None, None]

        cursor = (publisher._daily_digest_at(digest_now) - timedelta(days=1)).isoformat()
        await database.set_digest_cursor(111, cursor)

        assert await publisher.publish_pending() == (0, 0)
        assert len(channel.sent) == 3
        digest = channel.sent[-1]
        assert digest.title.startswith("Internship Daily Brief")
        assert "🔥 [Amazon" in digest.description
        assert "Example Corp" in digest.description
        assert "https://discord.com/channels/123/111/1000" in digest.description
        assert channel.contents[-1] == "<@&555>"
        assert "jobbot:digest:" in digest.footer.text

        assert await publisher.publish_pending() == (0, 0)
        assert len(channel.sent) == 3
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_digest_role_ping_requires_mention_everyone_permission(config_factory) -> None:
    config = config_factory()
    config.discord_internship_alert_role_id = 555
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        await _create_pending_delivery(database)
        permissions = SimpleNamespace(
            view_channel=True,
            send_messages=True,
            embed_links=True,
            read_message_history=True,
            mention_everyone=False,
        )
        channel = FakeChannel([], permissions=permissions)
        publisher = TrackChannelPublisher(config, database)
        local_now = datetime.now(ZoneInfo("America/New_York")).replace(
            hour=16, minute=5, second=0, microsecond=0
        )
        digest_now = local_now.astimezone(UTC)
        publisher._now = lambda: digest_now  # type: ignore[method-assign]
        publisher.bind_client(FakeClient(channel))  # type: ignore[arg-type]

        assert await publisher.publish_pending() == (1, 0)
        expired_cursor = (publisher._daily_digest_at(digest_now) - timedelta(days=1)).isoformat()
        await database.set_digest_cursor(111, expired_cursor)

        assert await publisher.publish_pending() == (0, 0)
        assert len(channel.sent) == 1
        assert await database.get_digest_cursor(111) == expired_cursor
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_digest_marker_reconciles_after_cursor_update_is_interrupted(
    config_factory,
) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        await _create_pending_delivery(database)
        publisher = TrackChannelPublisher(config, database)
        local_now = datetime.now(ZoneInfo("America/New_York")).replace(
            hour=16, minute=5, second=0, microsecond=0
        )
        digest_now = local_now.astimezone(UTC)
        publisher._now = lambda: digest_now  # type: ignore[method-assign]
        cursor = (publisher._daily_digest_at(digest_now) - timedelta(days=1)).isoformat()
        await database.set_digest_cursor(111, cursor)
        marker = publisher._digest_marker(111, publisher._daily_digest_at(digest_now))
        existing_digest = discord.Embed(title="Internship Daily Brief")
        existing_digest.set_footer(text=f"Job Bot • {marker}")
        existing_message = SimpleNamespace(
            id=777,
            author=SimpleNamespace(id=42),
            embeds=[existing_digest],
        )
        channel = FakeChannel([existing_message])
        publisher.bind_client(FakeClient(channel))  # type: ignore[arg-type]

        assert await publisher.publish_pending() == (1, 0)
        assert len(channel.sent) == 1  # The individual job post, not a duplicate digest.
        assert await database.get_digest_cursor(111) != cursor
    finally:
        await database.close()
