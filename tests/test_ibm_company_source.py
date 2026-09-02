from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from job_bot.company_sources.companies.ibm import IBMResponseError, IBMSource
from job_bot.company_sources.registry import create_company_source

FIXTURES = Path("tests/fixtures/company_sources")


class FakeHttpClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def post_json(
        self, url: str, **kwargs: Any
    ) -> tuple[object, dict[str, str], int]:
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected IBM API request")
        return self.responses.pop(0), {}, 200


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


def test_ibm_build_payload_filters_selected_early_career_areas() -> None:
    payload = IBMSource.build_payload()

    assert payload["size"] == 60
    assert payload["from"] == 0
    assert payload["sort"] == [{"dcdate": "desc"}, {"_score": "desc"}]
    assert payload["query"] == {
        "bool": {
            "must": [
                {
                    "terms": {
                        "field_keyword_08": [
                            "Software Engineering",
                            "Infrastructure & Technology",
                            "Data & Analytics",
                            "Cloud",
                        ]
                    }
                },
                {
                    "terms": {
                        "field_keyword_18": ["Internship", "Entry Level"]
                    }
                },
            ]
        }
    }
    assert "title" in payload["_source"]


@pytest.mark.asyncio
async def test_ibm_fetch_adapts_early_career_us_jobs() -> None:
    combined = _fixture("ibm_page_1.json")
    second = _fixture("ibm_page_2.json")
    assert isinstance(combined, dict) and isinstance(second, dict)
    combined["hits"]["hits"].extend(second["hits"]["hits"])
    http = FakeHttpClient([combined])
    source = IBMSource(http)  # type: ignore[arg-type]

    result = await source.fetch()

    assert result.source_id == "company_api_ibm"
    assert result.pages_checked == 1
    assert result.rejected == 0
    assert result.content_sha
    assert [call["json"]["from"] for call in http.calls] == [0]
    assert all(call["url"] == IBMSource.api_url for call in http.calls)
    assert all(call["headers"] == IBMSource.headers for call in http.calls)
    assert [job.external_id for job in result.observations] == ["87892", "99001"]
    assert [job.track for job in result.observations] == ["internship", "new_grad"]
    assert result.observations[0].application_url == (
        "https://careers.ibm.com/careers/JobDetail?jobid=87892"
    )
    assert result.observations[0].locations == ["Austin, TX"]
    assert result.observations[0].cycle is None
    assert result.observations[0].posted_date == "2026-08-15"
    assert result.observations[1].locations == ["United States"]
    assert result.observations[1].track == "new_grad"


@pytest.mark.asyncio
async def test_ibm_fetch_never_ingests_beyond_the_first_60_results() -> None:
    hits = [
        {
            "_id": f"ibm-hit-{job_id}",
            "_source": {
                "title": f"Software Engineer Intern {job_id}",
                "url": (
                    "https://careers.ibm.com/careers/JobDetail?"
                    f"jobid={job_id}"
                ),
                "field_keyword_05": "United States",
                "field_keyword_08": "Software Engineering",
                "field_keyword_18": "Internship",
                "field_keyword_19": "Austin, TX",
                "dcdate": "2026-08-15",
            },
        }
        for job_id in range(61)
    ]
    source = IBMSource(
        FakeHttpClient([{"hits": {"total": {"value": 61, "relation": "eq"}, "hits": hits}}])
    )  # type: ignore[arg-type]

    result = await source.fetch()

    assert len(result.observations) == 60
    assert result.observations[0].external_id == "0"
    assert result.observations[-1].external_id == "59"


@pytest.mark.asyncio
async def test_ibm_fetch_accepts_an_authoritative_empty_inventory() -> None:
    source = IBMSource(FakeHttpClient([_fixture("ibm_empty.json")]))  # type: ignore[arg-type]

    result = await source.fetch()

    assert result.pages_checked == 1
    assert result.observations == []
    assert result.rejected == 0


@pytest.mark.asyncio
async def test_ibm_fetch_rejects_inexact_or_malformed_totals() -> None:
    source = IBMSource(FakeHttpClient([_fixture("ibm_malformed.json")]))  # type: ignore[arg-type]

    with pytest.raises(IBMResponseError, match="must be exact"):
        await source.fetch()


def test_ibm_adapter_rejects_non_us_non_early_career_and_unsafe_jobs() -> None:
    source = IBMSource(FakeHttpClient([]))  # type: ignore[arg-type]
    base = {
        "_id": "one",
        "_source": {
            "title": "Software Engineer Intern",
            "url": "https://careers.ibm.com/careers/JobDetail?jobId=one",
            "field_keyword_05": "Canada",
            "field_keyword_08": "Software Engineering",
            "field_keyword_18": "Internship",
            "field_keyword_19": "Toronto, ON",
        },
    }
    assert source.to_observation(base) is None

    no_track = json.loads(json.dumps(base))
    no_track["_source"].update(
        title="Senior Software Engineer",
        field_keyword_05="United States",
        field_keyword_18="Professional",
        field_keyword_19="Austin, TX",
    )
    assert source.to_observation(no_track) is None

    unsafe = json.loads(json.dumps(base))
    unsafe["_source"].update(
        url="http://127.0.0.1/private",
        field_keyword_05="United States",
        field_keyword_19="Austin, TX",
    )
    assert source.to_observation(unsafe) is None

    wrong_career_area = json.loads(json.dumps(base))
    wrong_career_area["_source"].update(
        field_keyword_05="United States",
        field_keyword_08="Consulting",
        field_keyword_19="Austin, TX",
    )
    assert source.to_observation(wrong_career_area) is None


@pytest.mark.parametrize(
    "career_area",
    [
        "Software Engineering",
        "Infrastructure & Technology",
        "Data & Analytics",
        "Cloud",
    ],
)
def test_ibm_adapter_accepts_each_selected_career_area(career_area: str) -> None:
    source = IBMSource(FakeHttpClient([]))  # type: ignore[arg-type]
    raw_job = {
        "_id": "selected-area-intern",
        "_source": {
            "title": f"{career_area} Intern",
            "url": (
                "https://careers.ibm.com/careers/JobDetail?"
                "jobId=selected-area-intern"
            ),
            "field_keyword_05": "United States",
            "field_keyword_08": career_area,
            "field_keyword_18": "Internship",
            "field_keyword_19": "Austin, TX",
        },
    }

    observation = source.to_observation(raw_job)

    assert observation is not None
    assert observation.track == "internship"


def test_company_source_registry_constructs_ibm_and_rejects_unknown_keys() -> None:
    http = FakeHttpClient([])

    assert isinstance(create_company_source("ibm", http), IBMSource)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Unknown company source"):
        create_company_source("missing", http)  # type: ignore[arg-type]
