from __future__ import annotations

import asyncio

import pytest

from job_bot.feed_publisher import TrackChannelPublisher
from job_bot.mvp_runtime import FeedDiscordClient


@pytest.mark.asyncio
async def test_runtime_starts_only_the_core_poll_task() -> None:
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
    FeedDiscordClient._ensure_poll_task(
        harness,  # type: ignore[arg-type]
        name="core",
        pipeline=core_pipeline,  # type: ignore[arg-type]
        interval_seconds=300,
    )
    try:
        await asyncio.sleep(0)
        assert {(pipeline, interval, group) for pipeline, interval, group in started} == {
            (core_pipeline, 300, "core"),
        }
    finally:
        release.set()
        await asyncio.gather(*harness._poll_tasks.values())


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
