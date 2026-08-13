import json
from pathlib import Path

import pytest

from job_bot.github_feeds import (
    FeedDefinition,
    GitHubFeedFetcher,
    parse_intern_engine_json,
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
