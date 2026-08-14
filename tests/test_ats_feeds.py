from __future__ import annotations

import json

import pytest

from job_bot.ats_feeds import build_direct_ats_feeds, parse_direct_ats_json
from job_bot.github_feeds import FeedDefinition


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
    ],
)
def test_direct_ats_parsers_normalize_public_board_jobs(
    provider,
    payload,
    expected_track,
    expected_url,
) -> None:
    observations, rejected = parse_direct_ats_json(
        json.dumps(payload), _definition(provider)
    )

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
