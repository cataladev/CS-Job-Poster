from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from job_bot.company_sources.companies import nvidia
from job_bot.company_sources.companies.nvidia import NvidiaResponseError, NvidiaSource
from job_bot.company_sources.registry import create_company_source
from job_bot.company_sources.runner import CompanySourceRunner
from job_bot.database import Database
from job_bot.feed_pipeline import FeedPipeline

FIXTURES = Path(__file__).parent / "fixtures" / "company_sources"


class FakeHttpClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def get_json(self, url: str, **kwargs: Any) -> tuple[object, dict[str, str], int]:
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected NVIDIA API request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response, {}, 200


def _fixture(name: str = "nvidia_page_1.json") -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _page(positions: list[dict[str, Any]], *, count: int | None = None) -> dict[str, Any]:
    page = _fixture("nvidia_empty.json")
    page["data"].update(positions=positions, count=len(positions) if count is None else count)
    return page


@pytest.fixture
def fixed_today(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 2, 12, tzinfo=UTC)

    monkeypatch.setattr(nvidia, "datetime", FixedDatetime)


@pytest.mark.asyncio
async def test_fetch_paginates_and_maps_real_format_jobs(fixed_today):
    http = FakeHttpClient([_fixture(), _fixture("nvidia_page_2.json")])
    result = await NvidiaSource(http).fetch()

    assert result.source_id == "company_api_nvidia"
    assert result.pages_checked == 2
    assert result.rejected == 7
    assert len(result.content_sha) == 64
    assert [job.track for job in result.observations] == [
        "internship", "new_grad", "internship", "new_grad"
    ]
    job = result.observations[0]
    assert job.external_id == "JR2025792"
    assert job.company == "NVIDIA"
    assert job.title == "PhD Research Intern, Embodied and Agentic AI - 2027"
    assert job.locations == ["Santa Clara, CA, US", "Remote, CA, US"]
    assert job.application_url == "https://jobs.nvidia.com/careers/job/893397725773"
    assert job.posted_date == "2026-10-02"  # postedTs, not older creationTs
    assert job.source_url == NvidiaSource.source_url
    assert job.source_ref == "nvidia/JR2025792"
    assert job.raw_data == _fixture()["data"]["positions"][0]
    assert result.observations[-1].locations == ["Hillsboro, OR, US"]

    for call, start in zip(http.calls, ("0", "10")):
        assert call["url"] == NvidiaSource.api_url
        assert call["headers"]["Accept"] == "application/json"
        params = call["params"]
        assert dict(params)["domain"] == "nvidia.com"
        assert dict(params)["sort_by"] == "timestamp"
        assert dict(params)["start"] == start
        assert "pid" not in dict(params)
        assert [v for k, v in params if k == "filter_job_type"] == list(NvidiaSource.job_types)

    repeated = await NvidiaSource(FakeHttpClient([_fixture(), _fixture("nvidia_page_2.json")])).fetch()
    assert repeated.content_sha == result.content_sha


@pytest.mark.asyncio
async def test_empty_inventory_is_healthy():
    result = await NvidiaSource(FakeHttpClient([_fixture("nvidia_empty.json")])).fetch()
    assert result.observations == []
    assert result.rejected == 0
    assert result.pages_checked == 1


@pytest.mark.parametrize("change", [
    {"name": ""},
    {"id": None, "displayJobId": None, "atsJobId": None},
    {"positionUrl": None},
    {"positionUrl": "javascript:alert(1)"},
    {"positionUrl": "//evil.example.org/careers/job/123"},
    {"positionUrl": "https://user:password@jobs.nvidia.com/careers/job/123"},
    {"positionUrl": "/careers"},
    {"locations": [], "standardizedLocations": [], "workLocationOption": "remote"},
    {"locations": ["Israel, Tel Aviv"], "standardizedLocations": ["Tel Aviv, Tel Aviv, IL"]},
    {"locations": ["Canada, Toronto"], "standardizedLocations": ["Toronto, Ontario, CA"]},
    {"locations": ["Asti, Italy"], "standardizedLocations": ["Asti, Piedmont, IT"]},
    {"name": "Senior Software Engineer", "department": "Engineer, Sys SW"},
    {"name": "Manager, University Graduate Recruiting", "department": "Intern"},
    {"name": "Software Engineer", "department": "Engineer, Sys SW"},
    {"postedTs": None},
    {"postedTs": True},
    {"postedTs": "2026-10-02"},
    {"postedTs": float("nan")},
    {"postedTs": float("inf")},
    {"postedTs": -1},
])
def test_rejects_ineligible_and_malformed_records(change):
    position = {**_fixture()["data"]["positions"][0], **change}
    assert NvidiaSource(FakeHttpClient([])).to_observation(position) is None


@pytest.mark.parametrize("title", [
    "Systems Software Engineer - New College Grad 2026",
    "Research Scientist - New College Graduate 2027",
    "Software Engineer - New Grad 2027",
])
def test_new_college_grad_titles_route_without_changing_title(title):
    position = {**_fixture()["data"]["positions"][1], "name": title}
    job = NvidiaSource(FakeHttpClient([])).to_observation(position)
    assert job.track == "new_grad"
    assert job.title == title


def test_standardized_location_fallback_keeps_all_us_locations():
    position = {**_fixture()["data"]["positions"][0], "locations": [],
                "standardizedLocations": ["Santa Clara, CA, US", "Austin, TX, US", "CA,US", "Toronto, Ontario, CA"]}
    job = NvidiaSource(FakeHttpClient([])).to_observation(position)
    assert job.locations == ["Santa Clara, CA, US", "Austin, TX, US", "CA,US"]


@pytest.mark.parametrize("data", [
    None, [], {"status": 500, "error": "unavailable"},
    {"status": 200, "data": None},
    {"status": 200, "error": {"message": "unavailable", "body": ""}},
    {"status": 200, "error": "unavailable"},
])
def test_unexpected_payloads_fail_closed(data):
    with pytest.raises(NvidiaResponseError):
        NvidiaSource._read_page(data)


@pytest.mark.parametrize("field,value", [
    ("count", True), ("count", -1), ("count", "10"), ("count", None),
    ("positions", None), ("positions", ["not an object"]),
    ("sortBy", "relevance"), ("appliedFilters", {}),
])
def test_schema_and_filter_drift_fail_closed(field, value):
    page = _fixture("nvidia_empty.json")
    page["data"][field] = value
    with pytest.raises(NvidiaResponseError):
        NvidiaSource._read_page(page)


@pytest.mark.asyncio
async def test_stops_pagination_after_old_dates_but_keeps_boundary_page(fixed_today):
    position = {**_fixture()["data"]["positions"][0], "postedTs": 1790726400}  # Sep 30
    http = FakeHttpClient([_page([position], count=100)])
    result = await NvidiaSource(http).fetch()
    assert result.pages_checked == 1
    assert len(result.observations) == 1
    assert result.observations[0].posted_date == "2026-09-30"


@pytest.mark.asyncio
async def test_missing_date_does_not_stop_pagination(fixed_today):
    positions = _fixture()["data"]["positions"]
    http = FakeHttpClient([
        _page([{**positions[0], "postedTs": None}], count=2),
        _page([positions[1]], count=2),
    ])
    result = await NvidiaSource(http).fetch()
    assert result.pages_checked == 2
    assert result.rejected == 1
    assert len(result.observations) == 1


@pytest.mark.asyncio
async def test_pagination_advances_by_actual_length_and_is_bounded(fixed_today, monkeypatch):
    monkeypatch.setattr(NvidiaSource, "result_limit", 3)
    position = _fixture()["data"]["positions"][0]
    pages = [_page([{**position, "id": index}], count=100) for index in (1, 2, 3)]
    http = FakeHttpClient(pages)
    result = await NvidiaSource(http).fetch()
    assert [dict(call["params"])["start"] for call in http.calls] == ["0", "1", "2"]
    assert result.pages_checked == 3


@pytest.mark.asyncio
async def test_max_pages_bounds_requests(fixed_today, monkeypatch):
    monkeypatch.setattr(NvidiaSource, "max_pages", 1)
    result = await NvidiaSource(FakeHttpClient([_fixture()])).fetch()
    assert result.pages_checked == 1


@pytest.mark.asyncio
async def test_incomplete_empty_page_is_failure():
    with pytest.raises(NvidiaResponseError, match="empty page"):
        await NvidiaSource(FakeHttpClient([_page([], count=10)])).fetch()


@pytest.mark.asyncio
async def test_repeated_page_is_failure(fixed_today):
    position = _fixture()["data"]["positions"][0]
    page = _page([position], count=10)
    with pytest.raises(NvidiaResponseError, match="did not advance"):
        await NvidiaSource(FakeHttpClient([page, deepcopy(page)])).fetch()


@pytest.mark.asyncio
async def test_unsorted_results_are_failure(fixed_today):
    old = _fixture("nvidia_page_2.json")["data"]["positions"][0]
    new = _fixture()["data"]["positions"][0]
    with pytest.raises(NvidiaResponseError, match="newest first"):
        await NvidiaSource(FakeHttpClient([_page([old, new])])).fetch()


@pytest.mark.asyncio
async def test_later_request_failure_does_not_return_partial_snapshot(fixed_today):
    position = _fixture()["data"]["positions"][0]
    with pytest.raises(RuntimeError, match="unavailable"):
        await NvidiaSource(FakeHttpClient([
            _page([position], count=2), RuntimeError("unavailable")
        ])).fetch()


def test_registry_constructs_nvidia():
    assert isinstance(create_company_source("nvidia", FakeHttpClient([])), NvidiaSource)


class StubPublisher:
    async def publish_pending(self) -> tuple[int, int]:
        return 0, 0


@pytest.mark.asyncio
async def test_registered_adapter_uses_shared_dates_tracks_dedupe_and_feed_state(config_factory):
    config = config_factory()
    today = datetime.now(ZoneInfo(config.schedule_timezone)).date()
    today_ts = int(datetime(today.year, today.month, today.day, tzinfo=UTC).timestamp())
    positions = _fixture()["data"]["positions"][:3]
    positions[0]["postedTs"] = positions[1]["postedTs"] = today_ts
    positions[2]["postedTs"] = today_ts - 86400
    page = _page(positions)
    database = Database(config.sqlite_db_path)
    await database.initialize()
    pipeline = FeedPipeline(config=config, database=database, publisher=StubPublisher(),
                            feeds=(), direct_link_feeds=())
    runner = CompanySourceRunner(config=config, database=database, pipeline=pipeline)
    try:
        first = await runner.run("nvidia", FakeHttpClient([page]))
        assert first.baseline_completed
        assert first.canonical_created == 3
        assert first.deliveries_queued == 2
        assert first.deliveries_suppressed == 1
        assert await database.get_delivery_counts() == {"pending": 2, "suppressed": 1}
        assert len(await database.list_deliveries_for_send(111)) == 1
        assert len(await database.list_deliveries_for_send(222)) == 1
        state = await database.get_feed_state(NvidiaSource.source_id)
        assert state["baseline_complete"]
        assert state["repository"] == "company-api/nvidia"

        second = await runner.run("nvidia", FakeHttpClient([deepcopy(page)]))
        assert not second.baseline_completed
        assert second.duplicates == 3
        assert second.canonical_created == 0
        assert await database.get_delivery_counts() == {"pending": 2, "suppressed": 1}

        direct = NvidiaSource(FakeHttpClient([])).to_observation(positions[0])
        copy = replace(direct, source_id="jobright_internships", external_id="jobright-copy",
                       application_url="https://jobright.ai/jobs/info/nvidia-copy")
        merged = await pipeline.ingest_observations([copy], suppress_delivery=False)
        assert merged.duplicates == 1
        assert merged.canonical_created == 0
        distinct = replace(copy, external_id="jobright-austin", locations=["Austin, TX"])
        separate = await pipeline.ingest_observations([distinct], suppress_delivery=False)
        assert separate.canonical_created == 1
        assert await database.get_delivery_counts() == {"pending": 3, "suppressed": 1}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_failed_nvidia_snapshot_does_not_complete_baseline(config_factory):
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    pipeline = FeedPipeline(config=config, database=database, publisher=StubPublisher(),
                            feeds=(), direct_link_feeds=())
    runner = CompanySourceRunner(config=config, database=database, pipeline=pipeline)
    try:
        with pytest.raises(NvidiaResponseError):
            await runner.run("nvidia", FakeHttpClient([{"status": 500}]))
        state = await database.get_feed_state(NvidiaSource.source_id)
        assert not state["baseline_complete"]
        assert await database.get_delivery_counts() == {}
    finally:
        await database.close()
