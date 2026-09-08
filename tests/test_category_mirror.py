import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest

from outsource.mirror import OutsourceMirror
from test_feed_publisher_mvp import FakeChannel, FakeClient, FakeMessage

EXPECTED_CATEGORIES = [
    "SWE", "DATA / AI", "PM", "IT / SECURITY", "QUANT", "ANALYST", "HARDWARE", "DESIGN", "OTHER",
]


def setup_mirror(config_factory):
    config = config_factory()
    config.outsource_internship_channel_id = 333
    channel = FakeChannel([], channel_id=333)
    mirror = OutsourceMirror(config)
    mirror.bind_client(FakeClient({333: channel}))
    return mirror, channel


async def copy(mirror, title="Software Engineer", source_id=123):
    return await mirror.copy_message(
        track="internship", source_channel_id=111, source_message_id=source_id,
        content=None, embed=discord.Embed(title=title),
    )


@pytest.mark.asyncio
async def test_concurrent_jobs_share_card_and_each_category_gets_thread(config_factory):
    mirror, channel = setup_mirror(config_factory)
    await copy(mirror, "Unclear Internship")
    await asyncio.gather(copy(mirror), copy(mirror, "Backend Developer"))
    await copy(mirror, "Product Manager")
    assert [embed.title for embed in channel.sent] == EXPECTED_CATEGORIES
    assert [card.thread.name for card in channel.messages] == EXPECTED_CATEGORIES
    assert [len(card.thread.sent) for card in channel.messages] == [2, 0, 1, 0, 0, 0, 0, 0, 1]
    assert "Roles that belong here:" in channel.sent[0].description
    assert "backend" in channel.sent[0].description
    assert "don’t clearly fit" in channel.sent[-1].description
    assert all(mentions.users is False for mentions in channel.allowed_mentions)


@pytest.mark.asyncio
async def test_restart_reuses_old_archived_thread_without_a_date_cutoff(config_factory):
    mirror, channel = setup_mirror(config_factory)
    await copy(mirror)
    first_thread = channel.messages[0].thread
    channel.messages[0].created_at = datetime(2020, 1, 1, tzinfo=UTC)
    first_thread.archived = True
    restarted = OutsourceMirror(mirror.config)
    restarted.bind_client(mirror.client)
    await copy(restarted, "Backend Developer")
    assert len(channel.sent) == 9
    assert len(first_thread.sent) == 2
    await copy(restarted)
    assert len(channel.sent) == 9
    assert len(first_thread.sent) == 3
    assert first_thread.name == channel.sent[0].title == "SWE"
    assert channel.history_kwargs == {"limit": None, "oldest_first": True}


@pytest.mark.asyncio
async def test_recovers_card_when_thread_creation_was_interrupted(config_factory, monkeypatch):
    mirror, channel = setup_mirror(config_factory)
    original = FakeMessage.create_thread

    async def fail(self, **kwargs):
        raise asyncio.TimeoutError("thread creation interrupted")

    monkeypatch.setattr(FakeMessage, "create_thread", fail)
    assert await copy(mirror) is None
    assert len(channel.sent) == 1
    monkeypatch.setattr(FakeMessage, "create_thread", original)
    assert await copy(mirror) is not None
    assert len(channel.sent) == 9
    assert len(channel.messages[0].thread.sent) == 1


@pytest.mark.asyncio
async def test_recovers_card_after_discord_accepts_send_but_response_times_out(config_factory):
    mirror, channel = setup_mirror(config_factory)
    original = channel.send

    async def uncertain(**kwargs):
        await original(**kwargs)
        raise asyncio.TimeoutError("lost response")

    channel.send = uncertain
    assert await copy(mirror) is None
    channel.send = original
    assert await copy(mirror) is not None
    assert len(channel.sent) == 9


@pytest.mark.asyncio
async def test_does_not_reuse_a_users_category_marker(config_factory):
    mirror, channel = setup_mirror(config_factory)
    await copy(mirror)
    channel.messages[0].author = SimpleNamespace(id=99)
    mirror._cards.clear()
    mirror._prepared.clear()
    await copy(mirror)
    assert len(channel.sent) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", ["create_public_threads", "send_messages_in_threads", "read_message_history"])
async def test_missing_permissions_do_not_leave_stray_cards(config_factory, permission, caplog):
    mirror, channel = setup_mirror(config_factory)
    setattr(channel.permissions, permission, False)
    assert await copy(mirror) is None
    assert not channel.sent
    assert permission in caplog.text


@pytest.mark.asyncio
async def test_locked_thread_does_not_fall_back_to_noisy_parent_post(config_factory):
    mirror, channel = setup_mirror(config_factory)
    await copy(mirror)
    thread = channel.messages[0].thread
    thread.locked = True
    assert await copy(mirror) is None
    assert len(channel.sent) == 9
    assert len(thread.sent) == 1


@pytest.mark.asyncio
async def test_disabled_or_nonproduction_source_is_not_copied(config_factory):
    mirror, channel = setup_mirror(config_factory)
    mirror.config.outsource_internship_channel_id = None
    assert await copy(mirror) is None
    mirror.config.outsource_internship_channel_id = 333
    mirror.config.discord_channel_id = 999
    assert await copy(mirror) is None
    assert not channel.sent


@pytest.mark.asyncio
async def test_restart_recovers_each_category_and_creates_only_missing_categories(config_factory):
    mirror, channel = setup_mirror(config_factory)
    await copy(mirror)
    await copy(mirror, "Product Manager")
    first_threads = [card.thread for card in channel.messages]
    restarted = OutsourceMirror(mirror.config)
    restarted.bind_client(mirror.client)
    await copy(restarted, "Backend Developer")
    await copy(restarted, "Product Manager")
    assert len(channel.sent) == 9
    assert [len(thread.sent) for thread in first_threads] == [2, 0, 2, 0, 0, 0, 0, 0, 0]
    await copy(restarted, "Data Scientist")
    assert [embed.title for embed in channel.sent] == EXPECTED_CATEGORIES
    assert len({embed.footer.text for embed in channel.sent}) == 9


@pytest.mark.asyncio
async def test_legacy_daily_cards_are_not_mistaken_for_permanent_cards(config_factory):
    mirror, channel = setup_mirror(config_factory)
    embed = discord.Embed(title="OTHER 09/05/2026")
    embed.set_footer(text="jobbot:category:333:internship:other:2026-09-05")
    existing = await channel.send(embed=embed)
    await existing.create_thread(name=embed.title, auto_archive_duration=1440)
    await copy(mirror, "Unclear Internship")
    assert len(channel.sent) == 10
    assert not existing.thread.sent
    assert channel.sent[-1].title == "OTHER"
    assert channel.sent[-1].footer.text == "jobbot:category:333:internship:other"
    restarted = OutsourceMirror(mirror.config)
    restarted.bind_client(mirror.client)
    await copy(restarted, "Unclear Internship")
    assert len(channel.sent) == 10
    assert len(channel.messages[-1].thread.sent) == 2


@pytest.mark.asyncio
async def test_startup_creates_both_ordered_sets_once_without_listings(config_factory):
    mirror, channel = setup_mirror(config_factory)
    other_channel = FakeChannel([], channel_id=444)
    mirror.config.outsource_new_grad_channel_id = 444
    mirror.client.channels[444] = other_channel
    await asyncio.gather(mirror.ensure_categories(), mirror.ensure_categories())
    restarted = OutsourceMirror(mirror.config)
    restarted.bind_client(mirror.client)
    await restarted.ensure_categories()
    for destination in (channel, other_channel):
        assert [embed.title for embed in destination.sent] == EXPECTED_CATEGORIES
        assert [card.thread.name for card in destination.messages] == EXPECTED_CATEGORIES
        assert all(not card.thread.sent for card in destination.messages)
        assert all(not mentions.everyone for mentions in destination.allowed_mentions)


@pytest.mark.asyncio
async def test_startup_failure_does_not_block_other_track_and_retry_recovers(config_factory):
    mirror, channel = setup_mirror(config_factory)
    other_channel = FakeChannel([], channel_id=444)
    mirror.config.outsource_new_grad_channel_id = 444
    mirror.client.channels[444] = other_channel
    channel.send_error = asyncio.TimeoutError("unavailable")
    await mirror.ensure_categories()
    assert not channel.sent
    assert await copy(mirror) is None
    assert not channel.sent
    assert [embed.title for embed in other_channel.sent] == EXPECTED_CATEGORIES
    channel.send_error = None
    await mirror.ensure_categories()
    assert len(channel.sent) == len(other_channel.sent) == 9


@pytest.mark.asyncio
async def test_startup_recovers_an_interrupted_set_in_order(config_factory, monkeypatch):
    mirror, channel = setup_mirror(config_factory)
    original = FakeMessage.create_thread

    async def interrupt(self, **kwargs):
        if kwargs['name'] == 'PM':
            raise asyncio.TimeoutError("interrupted setup")
        return await original(self, **kwargs)

    monkeypatch.setattr(FakeMessage, "create_thread", interrupt)
    await mirror.ensure_categories()
    assert [embed.title for embed in channel.sent] == EXPECTED_CATEGORIES[:3]
    monkeypatch.setattr(FakeMessage, "create_thread", original)
    restarted = OutsourceMirror(mirror.config)
    restarted.bind_client(mirror.client)
    await restarted.ensure_categories()
    assert [embed.title for embed in channel.sent] == EXPECTED_CATEGORIES
    assert all(card.thread is not None for card in channel.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", [None, 111, 222])
async def test_startup_skips_disabled_and_primary_destinations(config_factory, destination):
    mirror, channel = setup_mirror(config_factory)
    mirror.config.outsource_internship_channel_id = destination
    mirror.config.discord_new_grad_channel_id = 222
    mirror.client.channels[111] = mirror.client.channels[222] = channel
    await mirror.ensure_categories()
    assert not channel.sent
    assert await copy(mirror) is None
    assert not channel.sent
