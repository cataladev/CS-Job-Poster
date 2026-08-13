from __future__ import annotations

import asyncio
from dataclasses import asdict
from types import SimpleNamespace

import discord
import pytest

from job_bot.database import Database
from job_bot.feed_publisher import SingleChannelPublisher
from job_bot.github_feeds import JobObservation
from job_bot.utils.feed_normalization import role_signature, sanitize_application_url
from job_bot.utils.normalization import normalize_job_posting


class FakeChannel:
    def __init__(self, messages, *, permissions=None, send_error=None):
        self.messages = messages
        self.sent = []
        self.guild = SimpleNamespace(me=object())
        self.type = discord.ChannelType.text
        self.permissions = permissions or SimpleNamespace(
            view_channel=True,
            send_messages=True,
            embed_links=True,
            read_message_history=True,
        )
        self.send_error = send_error

    def permissions_for(self, _member):
        return self.permissions

    async def send(self, *, embed):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(embed)
        return SimpleNamespace(id=999)

    def history(self, **_kwargs):
        async def generate():
            for message in self.messages:
                yield message

        return generate()


class FakeClient:
    def __init__(self, channel):
        self.user = SimpleNamespace(id=42)
        self.channel = channel

    def get_channel(self, _channel_id):
        return self.channel

    async def fetch_channel(self, _channel_id):
        return self.channel


async def _create_pending_delivery(database: Database) -> None:
    observation = JobObservation(
        source_id="source",
        source_label="Source",
        source_url="https://github.com/owner/source",
        external_id="job-1",
        company="Example Corp",
        title="Software Engineer Intern",
        locations=["Austin, TX"],
        application_url=sanitize_application_url("https://jobs.example.com/1").url,
        track="internship",
        cycle="Summer 2027",
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
        channel_id=111,
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
        publisher = SingleChannelPublisher(config, database)
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
async def test_both_tracks_use_the_single_configured_channel(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        publisher = SingleChannelPublisher(config, database)
        assert publisher.config.discord_channel_id == 111
        assert publisher.config.discord_new_grad_channel_id == 222
        # The MVP publisher has no track router; every outbox query uses the one channel.
        assert await database.list_deliveries_for_send(publisher.config.discord_channel_id) == []
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
        publisher = SingleChannelPublisher(config, database)
        publisher.bind_client(FakeClient(channel))  # type: ignore[arg-type]

        with pytest.raises(RuntimeError, match="read_message_history"):
            await publisher.publish_pending()

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
        publisher = SingleChannelPublisher(config, database)
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
        publisher = SingleChannelPublisher(config, database)

        embed = publisher.build_embed(job, marker=delivery["marker"], metadata=metadata)

        assert embed.url == "https://jobs.example.com/1"
        assert embed.description == "[Apply here](https://jobs.example.com/1)"
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
