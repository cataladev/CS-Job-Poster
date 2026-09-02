from __future__ import annotations

from datetime import datetime, timedelta
from typing import ClassVar
from zoneinfo import ZoneInfo

import pytest

from job_bot.company_sources.base import CompanySourceResult
from job_bot.company_sources.registry import COMPANY_SOURCE_REGISTRY
from job_bot.company_sources.runner import CompanySourceRunner
from job_bot.database import Database
from job_bot.feed_pipeline import FeedPipeline
from job_bot.github_feeds import JobObservation


class StubPublisher:
    async def publish_pending(self) -> tuple[int, int]:
        return 0, 0


class DummyHttpClient:
    pass


class MutableCompanySource:
    source_id = "company_api_test_corp"
    company_name = "Test Corp"
    source_url = "https://test.example.com/careers"
    observations: ClassVar[list[JobObservation]] = []
    error: Exception | None = None

    def __init__(self, http_client) -> None:
        self.http_client = http_client

    async def fetch(self) -> CompanySourceResult:
        if self.error is not None:
            raise self.error
        return CompanySourceResult(
            source_id=self.source_id,
            observations=list(self.observations),
            pages_checked=1,
            content_sha="snapshot",
        )


def _observation(
    external_id: str,
    *,
    source_id: str = MutableCompanySource.source_id,
    source_label: str = "Test Corp Careers",
    source_url: str = MutableCompanySource.source_url,
    application_url: str | None = None,
    title: str = "Software Engineer Intern",
    posted_date: str | None = None,
) -> JobObservation:
    return JobObservation(
        source_id=source_id,
        source_label=source_label,
        source_url=source_url,
        source_ref=f"test/{external_id}",
        external_id=external_id,
        company="Test Corp",
        title=title,
        locations=["Austin, TX"],
        application_url=application_url
        or f"https://test.example.com/jobs/{external_id}",
        track="internship",
        cycle="summer 2027",
        posted_date=(
            posted_date
            if posted_date is not None
            else datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        ),
    )


@pytest.mark.asyncio
async def test_company_source_first_snapshot_queues_only_same_day_roles(
    config_factory,
    monkeypatch,
) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    assert not await database.is_initial_feed_baseline_complete()
    pipeline = FeedPipeline(
        config=config,
        database=database,
        publisher=StubPublisher(),
        feeds=(),
        direct_link_feeds=(),
    )
    runner = CompanySourceRunner(config=config, database=database, pipeline=pipeline)
    monkeypatch.setitem(COMPANY_SOURCE_REGISTRY, "test_corp", MutableCompanySource)
    MutableCompanySource.error = None
    yesterday = (
        datetime.now(ZoneInfo(config.schedule_timezone)).date() - timedelta(days=1)
    ).isoformat()
    MutableCompanySource.observations = [
        _observation("one"),
        _observation(
            "old",
            title="Data Engineering Intern",
            posted_date=yesterday,
        ),
        _observation(
            "undated",
            title="Infrastructure Engineering Intern",
            posted_date="",
        ),
    ]

    try:
        first = await runner.run("test_corp", DummyHttpClient())  # type: ignore[arg-type]
        assert first.baseline_completed
        assert first.canonical_created == 3
        assert first.deliveries_queued == 1
        assert first.deliveries_suppressed == 2
        assert await database.get_delivery_counts() == {"pending": 1, "suppressed": 2}

        second = await runner.run("test_corp", DummyHttpClient())  # type: ignore[arg-type]
        assert not second.baseline_completed
        assert second.duplicates == 3
        assert await database.get_delivery_counts() == {"pending": 1, "suppressed": 2}

        MutableCompanySource.observations.append(
            _observation("two", title="Platform Engineering Intern")
        )
        third = await runner.run("test_corp", DummyHttpClient())  # type: ignore[arg-type]
        assert third.duplicates == 3
        assert third.canonical_created == 1
        assert third.deliveries_queued == 1
        assert await database.get_delivery_counts() == {"pending": 2, "suppressed": 2}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_company_source_uses_shared_cross_source_dedupe_and_direct_url_preference(
    config_factory,
    monkeypatch,
) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    assert not await database.is_initial_feed_baseline_complete()
    pipeline = FeedPipeline(
        config=config,
        database=database,
        publisher=StubPublisher(),
        feeds=(),
        direct_link_feeds=(),
    )
    runner = CompanySourceRunner(config=config, database=database, pipeline=pipeline)
    monkeypatch.setitem(COMPANY_SOURCE_REGISTRY, "test_corp", MutableCompanySource)
    MutableCompanySource.error = None
    MutableCompanySource.observations = [_observation("direct-one")]
    await database.record_feed_success(
        source_id=MutableCompanySource.source_id,
        repository="company-api/test_corp",
        path=MutableCompanySource.source_url,
        etag=None,
        content_sha="prior-baseline",
        last_modified=None,
    )
    jobright = _observation(
        "jobright-one",
        source_id="jobright_internships",
        source_label="Jobright",
        source_url="https://jobright.ai/jobs/software-engineer-intern-jobs-in-united-states",
        application_url="https://jobright.ai/jobs/info/jobright-one",
    )

    try:
        jobright_result = await pipeline.ingest_observations(
            [jobright],
            suppress_delivery=False,
        )
        assert jobright_result.canonical_created == 1
        assert jobright_result.deliveries_queued == 1

        direct_result = await runner.run("test_corp", DummyHttpClient())  # type: ignore[arg-type]
        assert direct_result.duplicates == 1
        assert direct_result.canonical_created == 0
        assert direct_result.deliveries_queued == 0
        jobs = await database.search_text("Test Corp")
        assert len(jobs) == 1
        assert jobs[0].canonical_url == "https://test.example.com/jobs/direct-one"
        assert await database.get_delivery_counts() == {"pending": 1}
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_baselined", [False, True])
async def test_company_ingest_error_keeps_healthy_rows_without_completing_snapshot(
    config_factory,
    monkeypatch,
    source_baselined,
) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    pipeline = FeedPipeline(
        config=config,
        database=database,
        publisher=StubPublisher(),
        feeds=(),
        direct_link_feeds=(),
    )
    runner = CompanySourceRunner(config=config, database=database, pipeline=pipeline)
    monkeypatch.setitem(COMPANY_SOURCE_REGISTRY, "test_corp", MutableCompanySource)
    monkeypatch.setattr(MutableCompanySource, "error", None)
    monkeypatch.setattr(
        MutableCompanySource,
        "observations",
        [
            _observation("broken", title="Data Engineering Intern"),
            _observation("healthy", title="Platform Engineering Intern"),
        ],
    )
    original_ingest = database.ingest_feed_job

    async def fail_one_observation(**kwargs):
        if kwargs["external_id"] == "broken":
            raise RuntimeError("simulated company row conflict")
        return await original_ingest(**kwargs)

    monkeypatch.setattr(database, "ingest_feed_job", fail_one_observation)
    try:
        if source_baselined:
            await database.record_feed_success(
                source_id=MutableCompanySource.source_id,
                repository="company-api/test_corp",
                path=MutableCompanySource.source_url,
                etag=None,
                content_sha="previous-snapshot",
                last_modified=None,
            )

        with pytest.raises(RuntimeError, match="1 observation ingestion error"):
            await runner.run("test_corp", DummyHttpClient())  # type: ignore[arg-type]

        state = await database.get_feed_state(MutableCompanySource.source_id)
        assert state is not None
        assert bool(state["baseline_complete"]) == source_baselined
        assert state["content_sha"] == (
            "previous-snapshot" if source_baselined else None
        )
        assert "simulated company row conflict" in state["last_error"]
        assert len(await database.search_text("Platform Engineering Intern")) == 1
        assert await database.search_text("Data Engineering Intern") == []
        delivery_state = "pending"
        assert await database.get_delivery_counts() == {delivery_state: 1}
        run = database._connection.execute(
            "SELECT * FROM source_runs WHERE source_name = ? ORDER BY id DESC LIMIT 1",
            (MutableCompanySource.source_id,),
        ).fetchone()
        assert run["status"] == "error"
        assert run["jobs_new"] == 1

        monkeypatch.setattr(database, "ingest_feed_job", original_ingest)
        recovered = await runner.run("test_corp", DummyHttpClient())  # type: ignore[arg-type]
        assert recovered.baseline_completed == (not source_baselined)
        assert recovered.duplicates == 1
        assert recovered.canonical_created == 1
        state = await database.get_feed_state(MutableCompanySource.source_id)
        assert state["last_error"] is None
        assert state["content_sha"] == "snapshot"
        assert await database.get_delivery_counts() == {delivery_state: 2}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_company_source_failure_is_recorded_without_completing_baseline(
    config_factory,
    monkeypatch,
) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    pipeline = FeedPipeline(
        config=config,
        database=database,
        publisher=StubPublisher(),
        feeds=(),
        direct_link_feeds=(),
    )
    runner = CompanySourceRunner(config=config, database=database, pipeline=pipeline)
    monkeypatch.setitem(COMPANY_SOURCE_REGISTRY, "test_corp", MutableCompanySource)
    MutableCompanySource.observations = []
    MutableCompanySource.error = RuntimeError("company API unavailable")

    try:
        with pytest.raises(RuntimeError, match="unavailable"):
            await runner.run("test_corp", DummyHttpClient())  # type: ignore[arg-type]
        state = await database.get_feed_state(MutableCompanySource.source_id)
        assert state is not None
        assert not state["baseline_complete"]
        assert state["last_error"] == "company API unavailable"
        run = database._connection.execute(
            "SELECT * FROM source_runs WHERE source_name = ?",
            (MutableCompanySource.source_id,),
        ).fetchone()
        assert run["status"] == "error"
        assert run["error"] == "company API unavailable"
        assert await database.get_delivery_counts() == {}
    finally:
        MutableCompanySource.error = None
        await database.close()
