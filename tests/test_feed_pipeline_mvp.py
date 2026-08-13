from __future__ import annotations

import asyncio

from job_bot.database import Database
from job_bot.feed_pipeline import FeedPipeline
from job_bot.github_feeds import (
    FeedDefinition,
    FeedFetchResult,
    JobObservation,
    parse_markdown_table,
)

import pytest


class StubPublisher:
    def __init__(self) -> None:
        self.calls = 0

    async def publish_pending(self):
        self.calls += 1
        return 0, 0


class StubFetcher:
    def __init__(self, snapshots):
        self.snapshots = snapshots

    async def fetch(self, definition, http_client, **_state):
        value = self.snapshots[definition.source_id]
        if isinstance(value, BaseException):
            raise value
        return FeedFetchResult(
            definition=definition,
            observations=value,
            rejected=0,
            etag=f'"{definition.source_id}"',
            last_modified=None,
            content_sha=definition.source_id,
        )


def _feed(source_id: str) -> FeedDefinition:
    return FeedDefinition(
        source_id,
        source_id,
        f"owner/{source_id}",
        "main",
        "README.md",
        "internship",
        parse_markdown_table,
        trusted_us=True,
    )


def _job(source_id: str, external_id: str, *, location: str = "Austin, TX") -> JobObservation:
    return JobObservation(
        source_id=source_id,
        source_label=source_id,
        source_url=f"https://github.com/owner/{source_id}",
        external_id=external_id,
        company=f"Example {external_id} Corp",
        title="Software Engineer Intern",
        locations=[location],
        application_url=f"https://jobs.example.com/{external_id}",
        track="internship",
        cycle="Summer 2027",
    )


@pytest.mark.asyncio
async def test_first_complete_snapshot_is_silent_then_new_job_queues(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    feeds = (_feed("one"), _feed("two"))
    publisher = StubPublisher()
    fetcher = StubFetcher({"one": [_job("one", "a")], "two": [_job("two", "b")]})
    pipeline = FeedPipeline(
        config=config,
        database=database,
        publisher=publisher,
        feeds=feeds,
        fetcher=fetcher,
    )
    try:
        baseline = await pipeline.run()

        assert baseline.baseline_completed
        assert baseline.deliveries_suppressed == 2
        assert await database.get_delivery_counts() == {"suppressed": 2}

        fetcher.snapshots["one"] = [_job("one", "a"), _job("one", "new")]
        update = await pipeline.run()

        assert update.deliveries_queued == 1
        assert await database.get_delivery_counts() == {"pending": 1, "suppressed": 2}

        repeated = await pipeline.run()
        assert repeated.deliveries_queued == 0
        assert await database.get_delivery_counts() == {"pending": 1, "suppressed": 2}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_partial_baseline_never_queues_jobs(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    feeds = (_feed("one"), _feed("two"))
    publisher = StubPublisher()
    fetcher = StubFetcher({"one": [_job("one", "a")], "two": RuntimeError("unavailable")})
    pipeline = FeedPipeline(
        config=config,
        database=database,
        publisher=publisher,
        feeds=feeds,
        fetcher=fetcher,
    )
    try:
        partial = await pipeline.run()
        assert not partial.baseline_completed
        assert partial.errors
        assert await database.get_delivery_counts() == {"suppressed": 1}

        fetcher.snapshots["one"] = [_job("one", "a"), _job("one", "arrived-during-baseline")]
        fetcher.snapshots["two"] = [_job("two", "b")]
        completed = await pipeline.run()
        assert completed.baseline_completed
        assert await database.get_delivery_counts() == {"suppressed": 3}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_overlapping_scheduler_trigger_is_skipped(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    feed = _feed("one")
    publisher = StubPublisher()

    class BlockingFetcher:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def fetch(self, definition, http_client, **_state):
            self.started.set()
            await self.release.wait()
            return FeedFetchResult(
                definition=definition,
                observations=[_job("one", "a")],
                rejected=0,
                etag='"one"',
                last_modified=None,
                content_sha="one",
            )

    fetcher = BlockingFetcher()
    pipeline = FeedPipeline(
        config=config,
        database=database,
        publisher=publisher,
        feeds=(feed,),
        fetcher=fetcher,
    )
    try:
        first_run = asyncio.create_task(pipeline.run())
        await fetcher.started.wait()

        overlap = await pipeline.run()
        assert overlap.errors == ["feed run already in progress"]

        fetcher.release.set()
        completed = await first_run
        assert completed.baseline_completed
        assert publisher.calls == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_restart_preserves_baseline_and_pending_identity(config_factory) -> None:
    config = config_factory()
    feed = _feed("one")

    first_database = Database(config.sqlite_db_path)
    await first_database.initialize()
    first_fetcher = StubFetcher({"one": [_job("one", "a")]})
    first_pipeline = FeedPipeline(
        config=config,
        database=first_database,
        publisher=StubPublisher(),
        feeds=(feed,),
        fetcher=first_fetcher,
    )
    baseline = await first_pipeline.run()
    assert baseline.baseline_completed
    await first_database.close()

    second_database = Database(config.sqlite_db_path)
    await second_database.initialize()
    second_fetcher = StubFetcher({"one": [_job("one", "a"), _job("one", "new")]})
    second_pipeline = FeedPipeline(
        config=config,
        database=second_database,
        publisher=StubPublisher(),
        feeds=(feed,),
        fetcher=second_fetcher,
    )
    update = await second_pipeline.run()
    assert update.deliveries_queued == 1
    await second_database.close()

    third_database = Database(config.sqlite_db_path)
    await third_database.initialize()
    third_pipeline = FeedPipeline(
        config=config,
        database=third_database,
        publisher=StubPublisher(),
        feeds=(feed,),
        fetcher=second_fetcher,
    )
    try:
        repeated = await third_pipeline.run()
        assert repeated.deliveries_queued == 0
        assert await third_database.get_delivery_counts() == {
            "pending": 1,
            "suppressed": 1,
        }
    finally:
        await third_database.close()
