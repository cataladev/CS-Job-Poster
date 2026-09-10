from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from job_bot.company_sources.companies.microsoft import (
    MicrosoftResponseError,
    MicrosoftSource,
)
from job_bot.company_sources.registry import create_company_source

FIXTURES = Path("tests/fixtures/company_sources")


class FakeHttpClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def get_json(
        self, url: str, **kwargs: Any
    ) -> tuple[object, dict[str, str], int]:
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected Microsoft API request")
        return self.responses.pop(0), {}, 200


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


def test_microsoft_build_params_filters_intern() -> None:
    params = MicrosoftSource.build_params(seniority="Intern", start=0)

    assert params["domain"] == "microsoft.com"
    assert params["location"] == "United States"
    assert params["filter_seniority"] == "Intern"
    assert params["sort_by"] == "timestamp"
    assert params["start"] == "0"


@pytest.mark.asyncio
async def test_microsoft_fetch_adapts_intern_us_jobs() -> None:
    fixture = _fixture("microsoft_page_1.json")
    # Two seniority filters => two requests, return fixture for intern, empty for new grad
    empty = _fixture("microsoft_empty.json")
    http = FakeHttpClient([fixture, empty])
    source = MicrosoftSource(http)  # type: ignore[arg-type]

    result = await source.fetch()

    assert result.source_id == "company_api_microsoft"
    assert result.pages_checked == 2
    assert result.content_sha
    assert len(http.calls) == 2
    assert http.calls[0]["params"]["filter_seniority"] == "Intern"
    assert http.calls[1]["params"]["filter_seniority"] == "Recent Graduate"
    assert len(result.observations) == 2
    assert result.observations[0].external_id == "1234567"
    assert result.observations[0].track == "internship"
    assert result.observations[0].application_url == (
        "https://apply.careers.microsoft.com/careers/job/1234567"
    )
    assert result.observations[0].locations == ["Redmond, WA, US"]
    assert result.observations[0].posted_date == "2026-08-15"
    assert result.observations[1].external_id == "7654321"
    assert result.observations[1].track == "internship"


@pytest.mark.asyncio
async def test_microsoft_fetch_new_grad_track() -> None:
    empty = _fixture("microsoft_empty.json")
    fixture = _fixture("microsoft_page_1.json")
    http = FakeHttpClient([empty, fixture])
    source = MicrosoftSource(http)  # type: ignore[arg-type]

    result = await source.fetch()

    assert len(result.observations) == 2
    assert all(obs.track == "new_grad" for obs in result.observations)


@pytest.mark.asyncio
async def test_microsoft_fetch_accepts_empty_inventory() -> None:
    empty = _fixture("microsoft_empty.json")
    http = FakeHttpClient([empty, empty])
    source = MicrosoftSource(http)  # type: ignore[arg-type]

    result = await source.fetch()

    assert result.pages_checked == 2
    assert result.observations == []
    assert result.rejected == 0


@pytest.mark.asyncio
async def test_microsoft_fetch_rejects_malformed_count() -> None:
    source = MicrosoftSource(
        FakeHttpClient([_fixture("microsoft_malformed.json")])
    )  # type: ignore[arg-type]

    with pytest.raises(MicrosoftResponseError, match="numeric count"):
        await source.fetch()


def test_microsoft_adapter_rejects_non_us_jobs() -> None:
    source = MicrosoftSource(FakeHttpClient([]))  # type: ignore[arg-type]
    position = {
        "id": "abc-123",
        "displayJobId": "9999999",
        "name": "Software Engineering Intern",
        "locations": ["London, United Kingdom"],
        "standardizedLocations": ["London, GB"],
        "postedTs": 1786795200,
        "positionUrl": "/careers/job/9999999",
    }
    assert source.to_observation(position, track="internship") is None


def test_microsoft_adapter_rejects_missing_title() -> None:
    source = MicrosoftSource(FakeHttpClient([]))  # type: ignore[arg-type]
    position = {
        "id": "abc-123",
        "displayJobId": "9999999",
        "name": "",
        "locations": ["Redmond, Washington, United States"],
        "standardizedLocations": ["Redmond, WA, US"],
        "postedTs": 1786795200,
        "positionUrl": "/careers/job/9999999",
    }
    assert source.to_observation(position, track="internship") is None


def test_microsoft_adapter_accepts_remote_us() -> None:
    source = MicrosoftSource(FakeHttpClient([]))  # type: ignore[arg-type]
    position = {
        "id": "abc-123",
        "displayJobId": "8888888",
        "name": "Software Engineering Intern",
        "locations": [],
        "standardizedLocations": [],
        "postedTs": 1786795200,
        "workLocationOption": "Remote",
        "positionUrl": "/careers/job/8888888",
    }
    obs = source.to_observation(position, track="internship")
    assert obs is not None
    assert obs.locations == ["United States (Remote)"]


def test_microsoft_posted_date_from_unix_timestamp() -> None:
    assert MicrosoftSource._posted_date(1786795200) == "2026-08-15"
    assert MicrosoftSource._posted_date(None) is None
    assert MicrosoftSource._posted_date("") is None


@pytest.mark.asyncio
async def test_microsoft_pagination() -> None:
    page1 = {
        "data": {
            "count": 25,
            "positions": [
                {
                    "id": f"id-{i}",
                    "displayJobId": str(1000 + i),
                    "name": f"Intern Role {i}",
                    "locations": ["Seattle, Washington, United States"],
                    "standardizedLocations": ["Seattle, WA, US"],
                    "postedTs": 1786795200,
                    "positionUrl": f"/careers/job/{1000 + i}",
                }
                for i in range(20)
            ],
        }
    }
    page2 = {
        "data": {
            "count": 25,
            "positions": [
                {
                    "id": f"id-{i}",
                    "displayJobId": str(1000 + i),
                    "name": f"Intern Role {i}",
                    "locations": ["Seattle, Washington, United States"],
                    "standardizedLocations": ["Seattle, WA, US"],
                    "postedTs": 1786795200,
                    "positionUrl": f"/careers/job/{1000 + i}",
                }
                for i in range(20, 25)
            ],
        }
    }
    empty = _fixture("microsoft_empty.json")
    http = FakeHttpClient([page1, page2, empty])
    source = MicrosoftSource(http)  # type: ignore[arg-type]

    result = await source.fetch()

    # Should have paginated: page1 (20 items, start=0) + page2 (5 items, start=20) for intern,
    # then empty for new grad
    assert result.pages_checked == 3
    assert len(result.observations) == 25
    assert http.calls[0]["params"]["start"] == "0"
    assert http.calls[1]["params"]["start"] == "20"


def test_company_source_registry_constructs_microsoft() -> None:
    http = FakeHttpClient([])
    source = create_company_source("microsoft", http)  # type: ignore[arg-type]
    assert isinstance(source, MicrosoftSource)
