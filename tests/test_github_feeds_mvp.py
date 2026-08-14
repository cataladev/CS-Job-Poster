import json
from pathlib import Path

import pytest

from job_bot.github_feeds import (
    FEEDS,
    FeedDefinition,
    GitHubFeedFetcher,
    parse_intern_engine_json,
    parse_jobright_next_data,
    parse_markdown_table,
    parse_simplify_json,
)


FIXTURES = Path("tests/fixtures")


def test_simplify_parser_keeps_only_active_explicit_us_rows() -> None:
    definition = FeedDefinition(
        "simplify_test",
        "Simplify",
        "owner/repo",
        "dev",
        "listings.json",
        "internship",
        parse_simplify_json,
    )
    jobs, rejected = parse_simplify_json(
        (FIXTURES / "feed_simplify.json").read_text(),
        definition,
    )

    assert len(jobs) == 1
    assert jobs[0].external_id == "us-1"
    assert jobs[0].cycle == "summer 2027"
    assert jobs[0].application_url == "https://jobs.example.com/roles/1"
    assert rejected == 1


def test_simplify_new_grad_cycle_uses_structured_terms() -> None:
    definition = FeedDefinition(
        "simplify_new_grad_test",
        "Simplify",
        "owner/repo",
        "dev",
        "listings.json",
        "new_grad",
        parse_simplify_json,
        cycle_hint="2027",
    )
    payload = json.dumps(
        [
            {
                "company_name": "Example Corp",
                "id": "ng-1",
                "title": "Software Engineer - New Grad",
                "active": True,
                "terms": ["2026"],
                "url": "https://jobs.example.com/new-grad/1",
                "locations": ["Austin, TX"],
            }
        ]
    )

    jobs, rejected = parse_simplify_json(payload, definition)

    assert rejected == 0
    assert jobs[0].cycle == "2026"


def test_markdown_parser_handles_html_and_markdown_apply_links() -> None:
    definition = FeedDefinition(
        "speedy_test",
        "SpeedyApply",
        "owner/repo",
        "main",
        "README.md",
        "internship",
        parse_markdown_table,
        trusted_us=True,
    )
    jobs, rejected = parse_markdown_table(
        (FIXTURES / "feed_speedy.md").read_text(),
        definition,
    )

    assert len(jobs) == 1
    assert jobs[0].company == "Example Corp"
    assert jobs[0].locations == ["Austin, TX"]
    assert jobs[0].application_url == "https://jobs.example.com/roles/1"
    assert rejected == 1


def test_markdown_parser_keeps_only_us_parts_of_mixed_location_rows() -> None:
    definition = FeedDefinition(
        "readme_test",
        "README",
        "owner/repo",
        "main",
        "README.md",
        "internship",
        parse_markdown_table,
        cycle_hint="Summer 2027",
    )
    markdown_text = """
| Company | Role | Location | Terms | Application | Age |
| --- | --- | --- | --- | --- | --- |
| 🔥 Example Corp ✓ | Software Intern | Toronto, Canada<br>Austin, TX | Summer 2027 | [Simplify](https://simplify.jobs/p/one?utm_source=test) [Direct](https://jobs.example.com/roles/one?utm_source=test) | 0d |
| ↳ | Platform Intern | Remote in Canada<br>Remote in USA | Summer 2027 | [Apply #1](https://jobs.example.com/roles/two?gh_jid=2&utm_campaign=x) [Apply #2](https://jobs.example.com/roles/three?gh_jid=3) | 1d |
"""

    jobs, rejected = parse_markdown_table(markdown_text, definition)

    assert rejected == 0
    assert len(jobs) == 3
    assert [job.company for job in jobs] == ["Example Corp"] * 3
    assert jobs[0].locations == ["Austin, TX"]
    assert jobs[0].application_url == "https://jobs.example.com/roles/one"
    assert jobs[1].locations == ["Remote in USA"]
    assert {job.application_url for job in jobs[1:]} == {
        "https://jobs.example.com/roles/two?gh_jid=2",
        "https://jobs.example.com/roles/three?gh_jid=3",
    }


def test_runtime_feeds_cover_only_us_job_surfaces() -> None:
    configured = {(feed.repository, feed.branch, feed.path) for feed in FEEDS}

    assert configured == {
        ("SimplifyJobs/Summer2027-Internships", "dev", "README.md"),
        ("SimplifyJobs/Summer2027-Internships", "dev", "README-Off-Season.md"),
        ("SimplifyJobs/New-Grad-Positions", "dev", "README.md"),
        ("speedyapply/2027-SWE-College-Jobs", "main", "README.md"),
        ("speedyapply/2027-SWE-College-Jobs", "main", "NEW_GRAD_USA.md"),
        ("sndsh404/summer-2027-internships", "main", "README.md"),
        (
            "zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships",
            "main",
            "README.md",
        ),
        (
            "zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships",
            "main",
            "docs/api/jobs.json",
        ),
        ("jobright.ai", "public", "software-engineer-intern-jobs-in-united-states"),
        ("jobright.ai", "public", "software-engineer-new-graduate-jobs-in-united-states"),
    }

    assert all("INTL" not in feed.path for feed in FEEDS)
    assert all(not feed.trusted_us for feed in FEEDS)


def test_jobright_parser_uses_public_us_listings_and_keeps_tracks_separate() -> None:
    internship_definition = FeedDefinition(
        "jobright_internships",
        "Jobright",
        "jobright.ai",
        "public",
        "software-engineer-intern-jobs-in-united-states",
        "internship",
        parse_jobright_next_data,
        fetch_url="https://jobright.ai/jobs/software-engineer-intern-jobs-in-united-states",
    )
    new_grad_definition = FeedDefinition(
        "jobright_new_grad",
        "Jobright",
        "jobright.ai",
        "public",
        "software-engineer-new-graduate-jobs-in-united-states",
        "new_grad",
        parse_jobright_next_data,
        fetch_url="https://jobright.ai/jobs/software-engineer-new-graduate-jobs-in-united-states",
    )
    payload = (FIXTURES / "feed_jobright.html").read_text()

    internships, internship_rejected = parse_jobright_next_data(payload, internship_definition)
    new_grads, new_grad_rejected = parse_jobright_next_data(payload, new_grad_definition)

    assert internship_rejected == 2
    assert len(internships) == 1
    assert internships[0].external_id == "intern-us-1"
    assert internships[0].locations == ["Austin, TX"]
    assert internships[0].cycle == "fall 2026"
    assert internships[0].posted_date == "2026-08-14"
    assert internships[0].application_url == "https://jobright.ai/jobs/info/intern-us-1"

    assert new_grad_rejected == 2
    assert len(new_grads) == 1
    assert new_grads[0].external_id == "grad-us-1"
    assert new_grads[0].cycle == "2027"
    assert new_grads[0].locations == ["United States"]


def test_intern_engine_parser_accepts_structured_jobs_object() -> None:
    definition = FeedDefinition(
        "engine_test",
        "Internship Engine",
        "owner/repo",
        "main",
        "docs/api/jobs.json",
        "internship",
        parse_intern_engine_json,
        trusted_us=True,
    )
    jobs, rejected = parse_intern_engine_json(
        (FIXTURES / "feed_intern_engine.json").read_text(),
        definition,
    )

    assert rejected == 0
    assert len(jobs) == 1
    assert jobs[0].external_id == "greenhouse:example:123"
    assert jobs[0].posted_date == "2026-08-12"
    assert jobs[0].application_url.endswith("?gh_jid=123")


@pytest.mark.parametrize("payload", ["", "{}", "[]", "not-json"])
def test_structured_parser_fails_closed_on_bad_or_empty_payload(payload: str) -> None:
    definition = FeedDefinition(
        "bad",
        "Bad",
        "owner/repo",
        "main",
        "jobs.json",
        "internship",
        parse_simplify_json,
    )
    with pytest.raises(ValueError):
        parse_simplify_json(payload, definition)


@pytest.mark.asyncio
async def test_fetcher_sends_validators_and_accepts_304() -> None:
    definition = FeedDefinition(
        "unchanged",
        "Unchanged",
        "owner/repo",
        "main",
        "jobs.json",
        "internship",
        parse_simplify_json,
    )

    class FakeHttpClient:
        headers = None

        async def get_text(self, _url, *, headers):
            self.headers = headers
            return "", {}, 304

    client = FakeHttpClient()
    result = await GitHubFeedFetcher().fetch(
        definition,
        client,  # type: ignore[arg-type]
        etag='"snapshot"',
        last_modified="Wed, 12 Aug 2026 12:00:00 GMT",
    )

    assert result.not_modified
    assert result.observations == []
    assert result.etag == '"snapshot"'
    assert client.headers == {
        "If-None-Match": '"snapshot"',
        "If-Modified-Since": "Wed, 12 Aug 2026 12:00:00 GMT",
    }
