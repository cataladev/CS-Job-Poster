from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

from job_bot.database import Database, RECENT_FEED_CATCHUP_STATE_KEY
from job_bot.feed_pipeline import FeedPipeline
from job_bot.github_feeds import (
    FeedDefinition,
    FeedFetchResult,
    JobObservation,
    parse_markdown_table,
)
from job_bot.utils.normalization import normalize_job_posting

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


def _feed(
    source_id: str,
    *,
    track: str = "internship",
    poll_interval_seconds: int = 0,
    required_for_baseline: bool = True,
) -> FeedDefinition:
    return FeedDefinition(
        source_id,
        source_id,
        f"owner/{source_id}",
        "main",
        "README.md",
        track,
        parse_markdown_table,
        trusted_us=True,
        poll_interval_seconds=poll_interval_seconds,
        required_for_baseline=required_for_baseline,
    )


def _job(
    source_id: str,
    external_id: str,
    *,
    location: str = "Austin, TX",
    track: str = "internship",
    posted_date: str | None = None,
    source_ref: str = "main/README.md",
    company: str | None = None,
    title: str | None = None,
    application_url: str | None = None,
) -> JobObservation:
    return JobObservation(
        source_id=source_id,
        source_label=source_id,
        source_url=f"https://github.com/owner/{source_id}",
        external_id=external_id,
        company=company or f"Example {external_id} Corp",
        title=title or ("Software Engineer Intern" if track == "internship" else "Software Engineer"),
        locations=[location],
        application_url=application_url or f"https://jobs.example.com/{external_id}",
        track=track,
        cycle="Summer 2027",
        posted_date=posted_date,
        source_ref=source_ref,
    )


@pytest.mark.asyncio
async def test_cached_direct_link_enriches_jobright_before_delivery(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    direct_feed = _feed("github_direct")
    jobright_feed = _feed("jobright_internships")
    posted_date = datetime.now(UTC).date().isoformat()
    direct = _job(
        direct_feed.source_id,
        "direct-1",
        company="Startup Corp",
        posted_date=posted_date,
        application_url="https://careers.startup.com/jobs/1",
    )
    jobright = _job(
        jobright_feed.source_id,
        "jobright-1",
        company="Startup Corp",
        posted_date=posted_date,
        application_url="https://jobright.ai/jobs/info/jobright-1",
    )
    pipeline = FeedPipeline(
        config=config,
        database=database,
        publisher=StubPublisher(),
        feeds=(direct_feed, jobright_feed),
        fetcher=StubFetcher({direct_feed.source_id: [direct], jobright_feed.source_id: [jobright]}),
    )
    try:
        assert (await pipeline.run()).baseline_completed
        jobs = await database.search_text("Startup Corp")
        assert len(jobs) == 1
        assert jobs[0].canonical_url == "https://careers.startup.com/jobs/1"
        metadata = await database.get_feed_observation_metadata(int(jobs[0].id or 0))
        assert metadata["jobright_fallback_url"] == "https://jobright.ai/jobs/info/jobright-1"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_targeted_ats_board_primes_jobright_direct_link_cache(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    jobright_feed = _feed("jobright_internships")
    direct_board = FeedDefinition(
        "direct_ashby_startup",
        "Startup Corp direct careers",
        "direct-ats/ashby",
        "public",
        "startup",
        "direct_ats",
        parse_markdown_table,
        fetch_url="https://api.ashbyhq.com/posting-api/job-board/startup",
        provider="ashby",
        company_name="Startup Corp",
        required_for_baseline=False,
    )
    posted_date = datetime.now(UTC).date().isoformat()
    jobright = _job(
        jobright_feed.source_id,
        "jobright-1",
        company="Startup Corp",
        posted_date=posted_date,
        application_url="https://jobright.ai/jobs/info/jobright-1",
    )
    direct = _job(
        direct_board.source_id,
        "direct-1",
        company="Startup Corp",
        posted_date=posted_date,
        application_url="https://jobs.ashbyhq.com/startup/direct-1",
    )
    pipeline = FeedPipeline(
        config=config,
        database=database,
        publisher=StubPublisher(),
        feeds=(jobright_feed,),
        direct_link_feeds=(direct_board,),
        fetcher=StubFetcher(
            {jobright_feed.source_id: [jobright], direct_board.source_id: [direct]}
        ),
    )
    try:
        stats = await pipeline.run()
        assert stats.direct_link_boards_fetched == 1
        jobs = await database.search_text("Startup Corp")
        assert jobs[0].canonical_url == "https://jobs.ashbyhq.com/startup/direct-1"
        assert await database.get_due_direct_ats_sources(
            [direct_board.source_id],
            stale_before=(datetime.now(UTC) - timedelta(hours=4)).isoformat(),
        ) == set()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_direct_link_cache_rejects_stale_location_and_ambiguous_matches(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    posted_date = datetime.now(UTC).date().isoformat()
    direct = normalize_job_posting(
        _job(
            "github_direct",
            "direct-1",
            company="Startup Corp",
            application_url="https://careers.startup.com/jobs/1",
        ).to_job_posting()
    )
    jobright = normalize_job_posting(
        _job(
            "jobright_internships",
            "jobright-1",
            company="Startup Corp",
            application_url="https://jobright.ai/jobs/info/jobright-1",
        ).to_job_posting()
    )
    try:
        await database.cache_direct_link(
            job=direct,
            source_id="github_direct",
            source_ref="main/README.md",
            posted_date=(datetime.now(UTC) - timedelta(days=4)).date().isoformat(),
        )
        assert await database.find_cached_direct_link(job=jobright, posted_date=posted_date) is None

        await database.cache_direct_link(
            job=direct,
            source_id="github_direct",
            source_ref="main/README.md",
            posted_date=posted_date,
        )
        jobright.normalized_locations = ["seattle wa"]
        assert await database.find_cached_direct_link(job=jobright, posted_date=posted_date) is None

        jobright.normalized_locations = ["austin tx"]
        duplicate_direct = normalize_job_posting(
            _job(
                "second_direct",
                "direct-2",
                company="Startup Corp",
                application_url="https://jobs.startup.com/openings/2",
            ).to_job_posting()
        )
        await database.cache_direct_link(
            job=duplicate_direct,
            source_id="second_direct",
            source_ref="main/README.md",
            posted_date=posted_date,
        )
        assert await database.find_cached_direct_link(job=jobright, posted_date=posted_date) is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_direct_link_cache_allows_same_year_when_only_one_source_names_season(
    config_factory,
) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    posted_date = datetime.now(UTC).date().isoformat()
    direct = normalize_job_posting(
        _job(
            "github_direct",
            "direct-1",
            company="Startup Corp",
            application_url="https://careers.startup.com/jobs/1",
        ).to_job_posting()
    )
    direct.season_term = "2027"
    jobright = normalize_job_posting(
        _job(
            "jobright_internships",
            "jobright-1",
            company="Startup Corp",
            application_url="https://jobright.ai/jobs/info/jobright-1",
        ).to_job_posting()
    )
    jobright.season_term = "summer 2027"
    try:
        await database.cache_direct_link(
            job=direct,
            source_id="github_direct",
            source_ref="main/README.md",
            posted_date=posted_date,
        )
        assert await database.find_cached_direct_link(job=jobright, posted_date=posted_date) == (
            "https://careers.startup.com/jobs/1"
        )
        direct.season_term = "winter 2027"
        await database.cache_direct_link(
            job=direct,
            source_id="github_direct_winter",
            source_ref="main/README.md",
            posted_date=posted_date,
        )
        # The explicit winter copy is ignored for a summer Jobright role; the
        # only remaining eligible direct URL is the year-only observation.
        assert await database.find_cached_direct_link(job=jobright, posted_date=posted_date) == (
            "https://careers.startup.com/jobs/1"
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_late_direct_observation_updates_canonical_without_touching_sent_delivery(
    config_factory,
) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    jobright_feed = _feed("jobright_internships")
    direct_feed = _feed("github_direct")
    posted_date = datetime.now(UTC).date().isoformat()
    jobright = _job(
        jobright_feed.source_id,
        "jobright-1",
        company="Startup Corp",
        posted_date=posted_date,
        application_url="https://jobright.ai/jobs/info/jobright-1",
    )
    direct = _job(
        direct_feed.source_id,
        "direct-1",
        company="Startup Corp",
        posted_date=posted_date,
        application_url="https://careers.startup.com/jobs/1",
    )
    fetcher = StubFetcher({jobright_feed.source_id: [jobright], direct_feed.source_id: []})
    pipeline = FeedPipeline(
        config=config,
        database=database,
        publisher=StubPublisher(),
        feeds=(jobright_feed, direct_feed),
        fetcher=fetcher,
    )
    try:
        assert (await pipeline.run()).baseline_completed
        original = (await database.search_text("Startup Corp"))[0]
        delivery = database._connection.execute(  # noqa: SLF001 - sent-delivery fixture
            "SELECT id FROM discord_deliveries WHERE canonical_id = ?", (original.id,)
        ).fetchone()
        await database.mark_delivery_sent(
            delivery_id=int(delivery["id"]),
            canonical_id=int(original.id or 0),
            track=original.track,
            channel_id=111,
            message_id=777,
        )

        fetcher.snapshots[direct_feed.source_id] = [direct]
        await pipeline.run()

        updated = (await database.search_text("Startup Corp"))[0]
        assert updated.canonical_url == "https://careers.startup.com/jobs/1"
        post = database._connection.execute(  # noqa: SLF001 - sent-delivery assertion
            "SELECT message_id FROM discord_posts WHERE canonical_id = ?", (updated.id,)
        ).fetchone()
        assert post["message_id"] == "777"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_jobright_only_delivery_waits_five_minutes_without_new_fetches(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    feed = _feed("jobright_internships")
    fetcher = StubFetcher({feed.source_id: [_job(feed.source_id, "baseline")]})
    pipeline = FeedPipeline(
        config=config,
        database=database,
        publisher=StubPublisher(),
        feeds=(feed,),
        fetcher=fetcher,
    )
    try:
        assert (await pipeline.run()).baseline_completed
        fetcher.snapshots[feed.source_id].append(_job(feed.source_id, "new"))
        update = await pipeline.run()
        assert update.deliveries_queued == 1
        assert await database.get_delivery_counts() == {"pending": 1, "suppressed": 1}
        assert await database.list_deliveries_for_send(111) == []

        database._connection.execute(  # noqa: SLF001 - deferred-delivery fixture
            "UPDATE discord_deliveries SET not_before = '' WHERE state = 'pending'"
        )
        database._connection.commit()  # noqa: SLF001 - deferred-delivery fixture
        assert len(await database.list_deliveries_for_send(111)) == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_existing_sqlite_database_adds_deferred_delivery_index(tmp_path) -> None:
    """A deployed database may predate the deferred-delivery column."""
    database_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE discord_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_id INTEGER NOT NULL,
            channel_id TEXT NOT NULL,
            state TEXT NOT NULL,
            marker TEXT NOT NULL UNIQUE,
            message_id TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            sent_at TEXT,
            UNIQUE(canonical_id, channel_id)
        )
        """
    )
    connection.close()

    database = Database(str(database_path))
    try:
        await database.initialize()
        assert "not_before" in database._connection.column_names("discord_deliveries")  # noqa: SLF001
        indexes = database._connection.execute("PRAGMA index_list(discord_deliveries)").fetchall()  # noqa: SLF001
        assert any(index[1] == "idx_discord_deliveries_ready" for index in indexes)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_company_blacklist_rejects_dice_reposts(config_factory) -> None:
    config = config_factory()
    config.company_blacklist = ["dice"]
    database = Database(config.sqlite_db_path)
    await database.initialize()
    feed = _feed("jobright_internships")
    pipeline = FeedPipeline(
        config=config,
        database=database,
        publisher=StubPublisher(),
        feeds=(feed,),
        fetcher=StubFetcher(
            {
                feed.source_id: [
                    _job(
                        feed.source_id,
                        "dice-repost",
                        company="Dice",
                    )
                ]
            }
        ),
    )
    try:
        stats = await pipeline.run()

        assert stats.rejected == 1
        assert stats.canonical_created == 0
        assert await database.get_delivery_counts() == {}
    finally:
        await database.close()


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
async def test_direct_ats_feeds_wait_for_their_own_poll_interval(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    feed = _feed("direct_greenhouse_example", poll_interval_seconds=900)
    fetcher = StubFetcher({feed.source_id: [_job(feed.source_id, "first")]})
    pipeline = FeedPipeline(
        config=config,
        database=database,
        publisher=StubPublisher(),
        feeds=(feed,),
        fetcher=fetcher,
    )
    fetch_calls = 0
    original_fetch = fetcher.fetch

    async def counted_fetch(*args, **kwargs):
        nonlocal fetch_calls
        fetch_calls += 1
        return await original_fetch(*args, **kwargs)

    fetcher.fetch = counted_fetch  # type: ignore[method-assign]
    try:
        first = await pipeline.run()
        second = await pipeline.run()

        assert first.baseline_completed
        assert second.feeds_checked == 1
        assert fetch_calls == 1
        assert await database.get_delivery_counts() == {"suppressed": 1}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_optional_direct_ats_failure_does_not_block_required_baseline(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    required_feed = _feed("required")
    optional_direct_feed = _feed(
        "direct_ashby_unavailable",
        required_for_baseline=False,
    )
    pipeline = FeedPipeline(
        config=config,
        database=database,
        publisher=StubPublisher(),
        feeds=(required_feed, optional_direct_feed),
        fetcher=StubFetcher(
            {
                required_feed.source_id: [_job(required_feed.source_id, "first")],
                optional_direct_feed.source_id: RuntimeError("board unavailable"),
            }
        ),
    )
    try:
        result = await pipeline.run()

        assert result.baseline_completed
        assert result.errors == []
        assert await database.is_initial_feed_baseline_complete()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_upgrade_promotes_only_recent_suppressed_jobs_once(config_factory) -> None:
    config = config_factory()
    config.recent_catchup_hours = 48
    database = Database(config.sqlite_db_path)
    await database.initialize()
    feed = _feed("one")
    recent = datetime.now(UTC).date().isoformat()
    old = (datetime.now(UTC) - timedelta(days=10)).date().isoformat()
    fetcher = StubFetcher(
        {
            "one": [
                _job("one", "recent", posted_date=recent),
                _job("one", "old", posted_date=old),
                _job(
                    "one",
                    "legacy-json-only",
                    posted_date=recent,
                    source_ref="main/listings.json",
                ),
            ]
        }
    )
    pipeline = FeedPipeline(
        config=config,
        database=database,
        publisher=StubPublisher(),
        feeds=(feed,),
        fetcher=fetcher,
    )
    try:
        assert (await pipeline.run()).baseline_completed
        assert await database.get_delivery_counts() == {"suppressed": 3}

        # Simulate a database created by the pre-catch-up deployment: its feed
        # baseline exists, but the new one-time migration marker does not.
        database._connection.execute(  # noqa: SLF001 - upgrade-state fixture
            "DELETE FROM app_state WHERE key = ?",
            (RECENT_FEED_CATCHUP_STATE_KEY,),
        )
        database._connection.commit()  # noqa: SLF001 - upgrade-state fixture

        upgraded = await pipeline.run()
        assert upgraded.deliveries_promoted == 1
        assert await database.get_delivery_counts() == {"pending": 1, "suppressed": 2}

        repeated = await pipeline.run()
        assert repeated.deliveries_promoted == 0
        assert await database.get_delivery_counts() == {"pending": 1, "suppressed": 2}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_fresh_baseline_never_replays_recent_inventory(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    feed = _feed("one")
    fetcher = StubFetcher(
        {
            "one": [
                _job(
                    "one",
                    "recent-at-install",
                    posted_date=datetime.now(UTC).date().isoformat(),
                )
            ]
        }
    )
    pipeline = FeedPipeline(
        config=config,
        database=database,
        publisher=StubPublisher(),
        feeds=(feed,),
        fetcher=fetcher,
    )
    try:
        assert (await pipeline.run()).baseline_completed
        assert (await pipeline.run()).deliveries_promoted == 0
        assert await database.get_delivery_counts() == {"suppressed": 1}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_pipeline_rejects_non_us_observation_before_ingestion(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    feed = _feed("one")
    fetcher = StubFetcher(
        {"one": [_job("one", "canada", location="Toronto, ON, Canada")]}
    )
    pipeline = FeedPipeline(
        config=config,
        database=database,
        publisher=StubPublisher(),
        feeds=(feed,),
        fetcher=fetcher,
    )
    try:
        result = await pipeline.run()

        assert result.baseline_completed
        assert result.rejected == 1
        assert result.canonical_created == 0
        assert await database.get_delivery_counts() == {}
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
async def test_ingestion_yields_to_gateway_heartbeat_between_rows(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    feed = _feed("one")
    fetcher = StubFetcher({"one": [_job("one", "a"), _job("one", "b")]})
    pipeline = FeedPipeline(
        config=config,
        database=database,
        publisher=StubPublisher(),
        feeds=(feed,),
        fetcher=fetcher,
    )
    callback_ran = False
    second_row_saw_callback = False
    calls = 0
    original_ingest = database.ingest_feed_job

    async def observed_ingest(**kwargs):
        nonlocal callback_ran, second_row_saw_callback, calls
        calls += 1
        if calls == 1:
            def mark_callback() -> None:
                nonlocal callback_ran
                callback_ran = True

            asyncio.get_running_loop().call_soon(mark_callback)
        elif calls == 2:
            second_row_saw_callback = callback_ran
        return await original_ingest(**kwargs)

    database.ingest_feed_job = observed_ingest  # type: ignore[method-assign]
    try:
        await pipeline.run()

        assert callback_ran
        assert second_row_saw_callback
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


@pytest.mark.asyncio
async def test_internship_and_new_grad_jobs_queue_to_separate_channels(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    feeds = (
        _feed("internships", track="internship"),
        _feed("new-grads", track="new_grad"),
    )
    fetcher = StubFetcher(
        {
            "internships": [_job("internships", "intern-1")],
            "new-grads": [_job("new-grads", "grad-1", track="new_grad")],
        }
    )
    pipeline = FeedPipeline(
        config=config,
        database=database,
        publisher=StubPublisher(),
        feeds=feeds,
        fetcher=fetcher,
    )
    try:
        assert (await pipeline.run()).baseline_completed

        fetcher.snapshots["internships"].append(_job("internships", "intern-2"))
        fetcher.snapshots["new-grads"].append(
            _job("new-grads", "grad-2", track="new_grad")
        )
        update = await pipeline.run()

        assert update.deliveries_queued == 2
        assert len(await database.list_deliveries_for_send(111)) == 1
        assert len(await database.list_deliveries_for_send(222)) == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_new_grad_jobs_are_suppressed_when_channel_is_not_separate(
    config_factory,
) -> None:
    config = config_factory()
    config.discord_new_grad_channel_id = config.discord_channel_id
    database = Database(config.sqlite_db_path)
    await database.initialize()
    feed = _feed("new-grads", track="new_grad")
    fetcher = StubFetcher(
        {"new-grads": [_job("new-grads", "grad-1", track="new_grad")]}
    )
    pipeline = FeedPipeline(
        config=config,
        database=database,
        publisher=StubPublisher(),
        feeds=(feed,),
        fetcher=fetcher,
    )
    try:
        assert (await pipeline.run()).baseline_completed
        fetcher.snapshots["new-grads"].append(
            _job("new-grads", "grad-2", track="new_grad")
        )

        update = await pipeline.run()

        assert update.deliveries_queued == 0
        assert await database.list_delivery_channel_ids_for_send() == []
        assert await database.get_delivery_counts() == {"suppressed": 2}
    finally:
        await database.close()
