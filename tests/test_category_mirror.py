import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord
import pytest

from outsource.mirror import OutsourceMirror
from test_feed_publisher_mvp import FakeChannel, FakeClient, FakeMessage


def setup_mirror(config_factory):
    config = config_factory()
    config.outsource_internship_channel_id = 333
    channel = FakeChannel([], channel_id=333)
    mirror = OutsourceMirror(config)
    mirror.bind_client(FakeClient({333: channel}))
    mirror._now = lambda: datetime(2026, 9, 5, 16, tzinfo=UTC)
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
    assert [embed.title for embed in channel.sent] == [
        "OTHER 09/05/2026", "SWE 09/05/2026", "PM 09/05/2026",
    ]
    assert [len(card.thread.sent) for card in channel.messages] == [1, 2, 1]
    assert "Roles that belong here:" in channel.sent[0].description
    assert "backend" in channel.sent[1].description
    assert "don’t clearly fit" in channel.sent[0].description
    assert all(mentions.users is False for mentions in channel.allowed_mentions)


@pytest.mark.asyncio
async def test_restart_reuses_archived_thread_and_next_local_day_creates_card(config_factory):
    mirror, channel = setup_mirror(config_factory)
    await copy(mirror)
    first_thread = channel.messages[0].thread
    first_thread.archived = True
    restarted = OutsourceMirror(mirror.config)
    restarted.bind_client(mirror.client)
    # Still September 5 in New York, although UTC has advanced to September 6.
    restarted._now = lambda: datetime(2026, 9, 6, 3, 59, tzinfo=UTC)
    await copy(restarted, "Backend Developer")
    assert len(channel.sent) == 1
    assert len(first_thread.sent) == 2
    restarted._now = lambda: datetime(2026, 9, 6, 4, 1, tzinfo=UTC)
    await copy(restarted)
    assert len(channel.sent) == 2
    assert channel.sent[1].title == "SWE 09/06/2026"


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
    assert len(channel.sent) == 1
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
    assert len(channel.sent) == 1


@pytest.mark.asyncio
async def test_does_not_reuse_a_users_category_marker(config_factory):
    mirror, channel = setup_mirror(config_factory)
    await copy(mirror)
    channel.messages[0].author = SimpleNamespace(id=99)
    mirror._cards.clear()
    await copy(mirror)
    assert len(channel.sent) == 2


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
    assert len(channel.sent) == 1
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
@pytest.mark.parametrize("rollover", [
    datetime(2026, 9, 6, 4, 1, tzinfo=UTC),  # 00:01 EDT
    datetime(2026, 12, 16, 5, 1, tzinfo=UTC),  # 00:01 EST
    datetime(2026, 3, 9, 4, 1, tzinfo=UTC),  # After spring DST transition
    datetime(2026, 11, 2, 5, 1, tzinfo=UTC),  # After fall DST transition
])
async def test_day_rolls_at_0001_and_creates_only_matching_categories(config_factory, rollover):
    mirror, channel = setup_mirror(config_factory)
    mirror._now = lambda: rollover - timedelta(minutes=2)
    await copy(mirror)
    old_title = channel.sent[0].title
    # 00:00:59 still uses the preceding posting day, even after restart.
    restarted = OutsourceMirror(mirror.config)
    restarted.bind_client(mirror.client)
    restarted._now = lambda: rollover - timedelta(seconds=1)
    await copy(restarted, "Backend Developer")
    assert len(channel.sent) == 1
    assert len(channel.messages[0].thread.sent) == 2
    restarted._now = lambda: rollover
    await copy(restarted)
    assert len(channel.sent) == 2
    assert channel.sent[-1].title != old_title
    assert channel.sent[-1].title == f"SWE {rollover:%m/%d/%Y}"
    assert len(channel.messages[1].thread.sent) == 1
    await copy(restarted, "Backend Developer")
    assert len(channel.sent) == 2
    assert len(channel.messages[1].thread.sent) == 2


@pytest.mark.asyncio
async def test_reuses_legacy_precreated_card_without_filling_other_categories(config_factory):
    mirror, channel = setup_mirror(config_factory)
    embed = discord.Embed(title="OTHER 09/05/2026")
    embed.set_footer(text="jobbot:category:333:internship:other:2026-09-05")
    existing = await channel.send(embed=embed)
    await existing.create_thread(name=embed.title, auto_archive_duration=1440)
    await copy(mirror, "Unclear Internship")
    assert len(channel.sent) == 1
    assert len(existing.thread.sent) == 1
    assert channel.history_kwargs["after"].astimezone(UTC) < datetime(2026, 9, 5, 3, 59, tzinfo=UTC)
