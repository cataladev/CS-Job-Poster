from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import discord
import pytest

from job_bot.feed_publisher import TrackChannelPublisher
from job_bot.mvp_runtime import FeedDiscordClient


@pytest.mark.asyncio
async def test_commands_are_synced_only_to_the_posting_guild() -> None:
    calls: list[tuple[str, int | None]] = []

    class FakeTree:
        def copy_global_to(self, *, guild) -> None:
            calls.append(("copy", guild.id))

        def clear_commands(self, *, guild) -> None:
            calls.append(("clear", None if guild is None else guild.id))

        async def sync(self, *, guild=None) -> None:
            calls.append(("sync", None if guild is None else guild.id))

    harness = SimpleNamespace(
        tree=FakeTree(),
        guilds=[discord.Object(id=123), discord.Object(id=456)],
    )

    await FeedDiscordClient._sync_commands_to_guild(
        harness,  # type: ignore[arg-type]
        123,
    )

    assert calls == [
        ("copy", 123),
        ("clear", None),
        ("sync", 123),
        ("sync", None),
        ("clear", 456),
        ("sync", 456),
    ]


@pytest.mark.asyncio
async def test_runtime_can_start_independent_core_and_company_source_poll_tasks() -> None:
    started: list[tuple[object, int, str]] = []
    release = asyncio.Event()

    class Harness:
        def __init__(self) -> None:
            self._poll_tasks: dict[str, asyncio.Task[None]] = {}

        async def _poll_loop(self, *, pipeline, interval_seconds, feed_group):
            started.append((pipeline, interval_seconds, feed_group))
            await release.wait()

    harness = Harness()
    core_pipeline = object()
    company_source_pipeline = object()
    FeedDiscordClient._ensure_poll_task(
        harness,  # type: ignore[arg-type]
        name="core",
        pipeline=core_pipeline,  # type: ignore[arg-type]
        interval_seconds=300,
    )
    FeedDiscordClient._ensure_poll_task(
        harness,  # type: ignore[arg-type]
        name="company_sources",
        pipeline=company_source_pipeline,  # type: ignore[arg-type]
        interval_seconds=3600,
    )
    try:
        await asyncio.sleep(0)
        assert {(pipeline, interval, group) for pipeline, interval, group in started} == {
            (core_pipeline, 300, "core"),
            (company_source_pipeline, 3600, "company_sources"),
        }
    finally:
        release.set()
        await asyncio.gather(*harness._poll_tasks.values())


@pytest.mark.asyncio
async def test_poll_loop_reports_each_completed_scrape_cycle() -> None:
    reports = []

    class FakeReporter:
        async def report(self, **payload) -> None:
            reports.append(payload)

    class Harness:
        def __init__(self) -> None:
            self._shutdown = asyncio.Event()
            self.scrape_status_reporter = FakeReporter()
            self.logger = logging.getLogger("test_poll_loop")

        def is_closed(self) -> bool:
            return False

    harness = Harness()
    stats = SimpleNamespace(deliveries_sent=2, errors=[])

    class Pipeline:
        async def run(self):
            harness._shutdown.set()
            return stats

    await FeedDiscordClient._poll_loop(
        harness,  # type: ignore[arg-type]
        pipeline=Pipeline(),  # type: ignore[arg-type]
        interval_seconds=300,
        feed_group="core",
    )

    assert len(reports) == 1
    assert reports[0]["feed_group"] == "core"
    assert reports[0]["stats"] is stats
    assert reports[0]["unexpected_error"] is None


@pytest.mark.asyncio
async def test_concurrent_collectors_serialize_discord_publishing(config_factory) -> None:
    publisher = TrackChannelPublisher(config_factory(), database=object())  # type: ignore[arg-type]
    active = 0
    maximum_active = 0
    first_started = asyncio.Event()
    release = asyncio.Event()

    async def controlled_publish() -> tuple[int, int]:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        first_started.set()
        await release.wait()
        active -= 1
        return 0, 0

    publisher._publish_pending = controlled_publish  # type: ignore[method-assign]
    first = asyncio.create_task(publisher.publish_pending())
    await first_started.wait()
    second = asyncio.create_task(publisher.publish_pending())
    await asyncio.sleep(0)
    assert maximum_active == 1

    release.set()
    await asyncio.gather(first, second)
    assert maximum_active == 1


def test_client_defaults_to_no_mentions_so_nothing_can_ping_everyone() -> None:
    # The baseline is "ping nothing". Each send site re-enables exactly the one
    # role or the specific members it means to mention, so no job title, company
    # name, or quoted upstream string can reach @everyone or @here.
    captured: dict[str, object] = {}

    original_init = discord.Client.__init__

    def recording_init(self, **kwargs) -> None:
        captured.update(kwargs)
        original_init(self, **kwargs)

    discord.Client.__init__ = recording_init  # type: ignore[method-assign]
    try:
        FeedDiscordClient(
            config=SimpleNamespace(discord_channel_id=1, discord_new_grad_channel_id=2),
            database=SimpleNamespace(),
            pipeline=SimpleNamespace(),
            company_source_pipeline=SimpleNamespace(),
            publisher=SimpleNamespace(),
        )
    except Exception:
        # Construction may fail later on the stubbed collaborators; the client
        # arguments are already captured by then.
        pass
    finally:
        discord.Client.__init__ = original_init  # type: ignore[method-assign]

    allowed = captured.get("allowed_mentions")
    assert allowed is not None
    assert allowed.everyone is False
    assert allowed.roles is False
    assert allowed.users is False
