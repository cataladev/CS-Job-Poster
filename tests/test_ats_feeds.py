from __future__ import annotations

import json

import pytest

from job_bot.ats_feeds import (
    build_direct_ats_feeds,
    parse_bytedance_json,
    parse_amazon_json,
    parse_rippling_json,
    parse_direct_ats_json,
)
from job_bot.github_feeds import FeedDefinition
from job_bot.github_feeds import parse_applyguy_json


def _definition(provider: str) -> FeedDefinition:
    return FeedDefinition(
        source_id=f"direct_{provider}_example",
        source_label="Example Corp direct careers",
        repository=f"direct-ats/{provider}",
        branch="public",
        path="example-corp",
        track="direct_ats",
        parser=parse_direct_ats_json,
        provider=provider,
        company_name="Example Corp",
    )


@pytest.mark.parametrize(
    ("provider", "payload", "expected_track", "expected_url"),
    [
        (
            "greenhouse",
            {
                "jobs": [
                    {
                        "id": 1,
                        "title": "Software Engineering Intern, Summer 2027",
                        "location": {"name": "Austin, TX"},
                        "absolute_url": "https://job-boards.greenhouse.io/example/jobs/1",
                        "content": "Build distributed systems.",
                        "updated_at": "2026-08-14T12:00:00Z",
                    }
                ]
            },
            "internship",
            "https://job-boards.greenhouse.io/example/jobs/1",
        ),
        (
            "lever",
            [
                {
                    "id": "lever-1",
                    "text": "Software Engineer, New Grad 2027",
                    "categories": {"location": "Seattle, WA", "commitment": "Full-time"},
                    "hostedUrl": "https://jobs.lever.co/example/lever-1",
                    "descriptionPlain": "Early career engineering role.",
                    "createdAt": 1786708800000,
                }
            ],
            "new_grad",
            "https://jobs.lever.co/example/lever-1",
        ),
        (
            "ashby",
            {
                "jobs": [
                    {
                        "id": "ashby-1",
                        "title": "Software Engineer Intern",
                        "location": "San Francisco, CA",
                        "jobUrl": "/example-corp/ashby-1",
                        "descriptionPlain": "Internship program.",
                        "publishedAt": "2026-08-14T12:00:00Z",
                    }
                ]
            },
            "internship",
            "https://jobs.ashbyhq.com/example-corp/ashby-1",
        ),
        (
            "smartrecruiters",
            {
                "content": [
                    {
                        "id": "smart-1",
                        "name": "Entry Level Software Engineer",
                        "location": {
                            "fullLocation": "Boston, MA, United States",
                        },
                        "typeOfEmployment": {"label": "Full-time"},
                        "releasedDate": "2026-08-14T12:00:00Z",
                    }
                ]
            },
            "new_grad",
            "https://jobs.smartrecruiters.com/example-corp/smart-1",
        ),
        (
            "workday",
            {
                "jobPostings": [
                    {
                        "externalPath": "/job/Austin-TX/Software-Engineer-Intern_REQ-1",
                        "title": "Software Engineer Intern, Summer 2027",
                        "locationsText": "US, TX, Austin",
                        "postedOn": "Posted Today",
                        "bulletFields": ["Internship"],
                    }
                ]
            },
            "internship",
            "https://example.wd5.myworkdayjobs.com/ExampleExternal/job/Austin-TX/Software-Engineer-Intern_REQ-1",
        ),
        (
            "oracle",
            {
                "items": [
                    {
                        "requisitionList": [
                            {
                                "Id": "1234",
                                "Title": "Software Engineering Intern, Summer 2027",
                                "PrimaryLocation": "Austin, TX, United States",
                                "PostedDate": "2026-08-14",
                                "ShortDescriptionStr": "Internship program.",
                            }
                        ]
                    }
                ]
            },
            "internship",
            "https://example.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1234",
        ),
    ],
)
def test_direct_ats_parsers_normalize_public_board_jobs(
    provider,
    payload,
    expected_track,
    expected_url,
) -> None:
    definition = _definition(provider)
    if provider == "workday":
        definition = FeedDefinition(
            **{
                **definition.__dict__,
                "path": json.dumps(
                    {
                        "host": "example.wd5.myworkdayjobs.com",
                        "site": "ExampleExternal",
                        "tenant": "example",
                    },
                    sort_keys=True,
                ),
            }
        )
    if provider == "oracle":
        definition = FeedDefinition(
            **{
                **definition.__dict__,
                "path": json.dumps(
                    {"host": "example.fa.us2.oraclecloud.com", "site": "CX_1"},
                    sort_keys=True,
                ),
            }
        )
    observations, rejected = parse_direct_ats_json(json.dumps(payload), definition)

    assert rejected == 0
    assert len(observations) == 1
    observation = observations[0]
    assert observation.company == "Example Corp"
    assert observation.track == expected_track
    assert observation.application_url == expected_url
    assert observation.locations


def test_direct_ats_builder_uses_all_supported_seed_providers() -> None:
    feeds = build_direct_ats_feeds(
        "data/company_watchlist_seed.csv",
        poll_interval_seconds=900,
    )

    assert len(feeds) >= 300
    assert {feed.provider for feed in feeds} == {
        "greenhouse",
        "lever",
        "ashby",
        "smartrecruiters",
        "workday",
        "oracle",
        "optiver",
        "bytedance",
        "rippling",
        "amazon",
    }
    assert len({feed.source_id for feed in feeds}) == len(feeds)
    assert all(feed.poll_interval_seconds == 900 for feed in feeds)
    assert all(not feed.required_for_baseline for feed in feeds)


def test_direct_ats_parser_ignores_non_early_career_and_non_us_jobs() -> None:
    payload = {
        "jobs": [
            {
                "id": 1,
                "title": "Senior Software Engineer",
                "location": {"name": "Austin, TX"},
                "absolute_url": "https://job-boards.greenhouse.io/example/jobs/1",
            },
            {
                "id": 2,
                "title": "Software Engineering Intern",
                "location": {"name": "Toronto, Canada"},
                "absolute_url": "https://job-boards.greenhouse.io/example/jobs/2",
            },
        ]
    }

    observations, rejected = parse_direct_ats_json(
        json.dumps(payload), _definition("greenhouse")
    )

    assert observations == []
    assert rejected == 0


def test_direct_ats_parser_routes_explicit_new_grad_title_despite_internship_text() -> None:
    payload = {
        "jobs": [
            {
                "id": "notion-new-grad",
                "title": "Software Engineer, New Grad (Dec 2026)",
                "location": "San Francisco, California",
                "jobUrl": "/notion/notion-new-grad",
                "employmentType": "FullTime",
                "descriptionPlain": (
                    "Build internal tools and occasionally support the internship program."
                ),
            },
            {
                "id": "notion-recruiting",
                "title": "Head of Early Career Recruiting",
                "location": "San Francisco, California",
                "jobUrl": "/notion/notion-recruiting",
                "employmentType": "FullTime",
            },
        ]
    }

    observations, rejected = parse_direct_ats_json(
        json.dumps(payload), _definition("ashby")
    )

    assert rejected == 0
    assert [(job.external_id, job.track) for job in observations] == [
        ("notion-new-grad", "new_grad"),
    ]


def test_bytedance_parser_uses_public_direct_role_url() -> None:
    definition = FeedDefinition(
        source_id="direct_bytedance",
        source_label="ByteDance direct careers",
        repository="direct-ats/bytedance",
        branch="public",
        path="en",
        track="direct_ats",
        parser=parse_bytedance_json,
        provider="bytedance",
        company_name="ByteDance",
    )
    payload = {
        "data": {
            "job_post_list": [
                {
                    "id": "123",
                    "title": "Software Engineer Intern - 2027 Start",
                    "description": "Internship program.",
                    "recruit_type": {"en_name": "Intern"},
                    "city_info": {
                        "en_name": "San Jose",
                        "parent": {
                            "en_name": "California",
                            "parent": {"en_name": "United States of America"},
                        },
                    },
                }
            ]
        }
    }
    observations, rejected = parse_bytedance_json(json.dumps(payload), definition)
    assert rejected == 0
    assert len(observations) == 1
    assert observations[0].application_url == "https://joinbytedance.com/search/123"
    assert observations[0].locations == ["San Jose, California"]


def test_rippling_parser_accepts_only_us_early_career_roles() -> None:
    definition = FeedDefinition(
        source_id="direct_rippling",
        source_label="Rippling direct careers",
        repository="direct-ats/rippling",
        branch="public",
        path="rippling",
        track="direct_ats",
        parser=parse_rippling_json,
        provider="rippling",
        company_name="Rippling",
    )
    payload = {
        "results": [
            {
                "hits": [
                    {
                        "jobId": "us-role",
                        "name": "Software Engineer Intern - Winter 2027",
                        "url": "https://ats.rippling.com/rippling/jobs/us-role",
                        "locations": [{"name": "San Francisco, CA", "countryCode": "US"}],
                    },
                    {
                        "jobId": "non-us-role",
                        "name": "Software Engineer Intern - Winter 2027",
                        "url": "https://ats.rippling.com/rippling/jobs/non-us-role",
                        "locations": [{"name": "Bangalore, India", "countryCode": "IN"}],
                    },
                ]
            }
        ]
    }

    observations, rejected = parse_rippling_json(json.dumps(payload), definition)

    assert rejected == 0
    assert len(observations) == 1
    assert observations[0].title == "Software Engineer Intern - Winter 2027"
    assert observations[0].locations == ["San Francisco, CA"]
    assert observations[0].application_url == "https://ats.rippling.com/rippling/jobs/us-role"


def test_amazon_parser_normalizes_public_us_role() -> None:
    definition = FeedDefinition(
        source_id="direct_amazon",
        source_label="Amazon direct careers",
        repository="direct-ats/amazon",
        branch="public",
        path="amazon",
        track="direct_ats",
        parser=parse_amazon_json,
        provider="amazon",
        company_name="Amazon",
    )
    payload = {
        "jobs": [
            {
                "id": "role-1",
                "title": "Software Development Engineer Intern - Fall 2026",
                "job_path": "/en/jobs/123/engineer-intern",
                "location": "US, WA, Seattle",
                "posted_date": "August 14, 2026",
            }
        ]
    }

    observations, rejected = parse_amazon_json(json.dumps(payload), definition)

    assert rejected == 0
    assert len(observations) == 1
    assert observations[0].locations == ["Seattle, WA"]
    assert observations[0].application_url == "https://www.amazon.jobs/en/jobs/123/engineer-intern"
    assert observations[0].posted_date == "2026-08-14"


def test_applyguy_parser_uses_listing_url_not_aggregator_landing_page() -> None:
    definition = FeedDefinition(
        source_id="applyguy_internships",
        source_label="ApplyGuy direct listings",
        repository="ApplyGuy/2027-Internships",
        branch="main",
        path="data/internships.json",
        track="internship",
        parser=parse_applyguy_json,
    )
    payload = {
        "jobs": [
            {
                "id": "role-1",
                "company": "Example Corp",
                "title": "Software Engineer Intern (Summer 2027)",
                "location": "Austin, TX",
                "season": "Summer 2027",
                "posted": "2026-08-15",
                "url": "https://applyguy.ai/jobs?company=Example",
                "listingUrl": "https://jobs.example.com/roles/role-1",
            }
        ]
    }

    observations, rejected = parse_applyguy_json(json.dumps(payload), definition)

    assert rejected == 0
    assert observations[0].application_url == "https://jobs.example.com/roles/role-1"
