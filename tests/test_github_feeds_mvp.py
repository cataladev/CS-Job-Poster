import asyncio
import json
from pathlib import Path

import aiohttp
import pytest

from job_bot.feed_health import _format_duration
from job_bot.github_feeds import (
    FEEDS,
    JOBRIGHT_BLOCK_BACKOFF_STEPS,
    JOBRIGHT_MAX_EGRESS_ATTEMPTS,
    JOBRIGHT_POLL_INTERVAL_SECONDS,
    JOBRIGHT_PUBLIC_WINDOW_SIZE,
    JOBRIGHT_TAXONOMY_IDS,
    build_jobright_public_query,
    FeedDefinition,
    GitHubFeedFetcher,
    classify_jobright_failure,
    jobright_backoff_seconds,
    parse_intern_engine_json,
    parse_jobright_next_data,
    parse_jobright_public_cards,
    parse_markdown_table,
    parse_simplify_json,
)
from job_bot.utils.http import ProxyEndpoint


FIXTURES = Path("tests/fixtures")


def _jobright_card(
    job_id: str,
    *,
    title: str = "Software Engineer Intern",
    seniority: str = "Intern",
    employment_type: str = "Internship",
    repost: bool = False,
    original_url: str | None = None,
) -> dict[str, object]:
    return {
        "companyResult": {"companyName": "Example Corp"},
        "jobResult": {
            "jobId": job_id,
            "jobTitle": title,
            "jobSeniority": seniority,
            "jobLocation": "Austin, TX",
            "jobLocations": ["Austin, TX"],
            "applyLink": f"https://jobright.ai/jobs/info/{job_id}",
            "originalUrl": original_url,
            "employmentType": employment_type,
            "internHireDate": "Summer 2027",
            "publishTime": "2026-08-14 13:39:51",
            "repost": repost,
        },
    }


def _jobright_html(query: dict[str, object], cards: list[dict[str, object]]) -> str:
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": {"query": query, "jobList": cards}}})
        + "</script>"
    )


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
        ("ApplyGuy/2027-Internships", "main", "data/internships.json"),
        ("ApplyGuy/2027-New-Grad-Jobs", "main", "data/new-grad-jobs.json"),
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
        ("jobright.ai", "public", "taxonomy-window/internship"),
        ("jobright.ai", "public", "taxonomy-window/new-grad"),
    }

    assert all("INTL" not in feed.path for feed in FEEDS)
    assert all(not feed.trusted_us for feed in FEEDS)
    assert {feed.provider for feed in FEEDS if feed.repository == "jobright.ai"} == {"jobright"}
    # Jobright publishes continuously and is polled every minute; it is the reason
    # the collection loop ticks that fast. Every other feed states a slower cadence of
    # its own so the faster loop does not drag the repository feeds along with it.
    assert {
        feed.poll_interval_seconds
        for feed in FEEDS
        if feed.repository == "jobright.ai"
    } == {60}
    assert all(
        feed.poll_interval_seconds == 300
        for feed in FEEDS
        if feed.repository != "jobright.ai"
    )


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
    # Jobright publishes its own index timestamp, not the employer's posting date,
    # so no date is carried rather than a wrong one.
    assert internships[0].posted_date is None
    assert internships[0].raw_data["jobright_publish_time"] == "2026-08-14 13:39:51"
    assert internships[0].application_url == "https://jobright.ai/jobs/info/intern-us-1"

    assert new_grad_rejected == 2
    assert len(new_grads) == 1
    assert new_grads[0].external_id == "grad-us-1"
    assert new_grads[0].cycle == "2027"
    assert new_grads[0].locations == ["United States"]


def test_jobright_new_grad_parser_keeps_explicit_entry_level_and_engineer_one_roles() -> None:
    definition = FeedDefinition(
        "jobright_new_grad",
        "Jobright",
        "jobright.ai",
        "public",
        "software-engineer-new-graduate-jobs-in-united-states",
        "new_grad",
        parse_jobright_next_data,
        fetch_url="https://jobright.ai/jobs/software-engineer-new-graduate-jobs-in-united-states",
        provider="jobright",
    )
    cards = [
        _jobright_card(
            "entry-level-1",
            title="Entry Level Platform Engineer",
            seniority="Entry Level",
            employment_type="Full-time",
        ),
        _jobright_card(
            "engineer-one-1",
            title="Software Engineer 1 - Commerce Engineering",
            seniority="Entry Level",
            employment_type="Full-time",
        ),
    ]

    jobs, rejected = parse_jobright_public_cards(cards, definition)

    assert rejected == 0
    assert [job.external_id for job in jobs] == ["entry-level-1", "engineer-one-1"]


def test_jobright_parser_skips_native_reposts() -> None:
    definition = FeedDefinition(
        "jobright_internships",
        "Jobright",
        "jobright.ai",
        "public",
        "software-engineer-intern-jobs-in-united-states",
        "internship",
        parse_jobright_next_data,
        fetch_url="https://jobright.ai/jobs/software-engineer-intern-jobs-in-united-states",
        provider="jobright",
    )
    jobs, rejected = parse_jobright_public_cards(
        [_jobright_card("fresh"), _jobright_card("reposted", repost=True)],
        definition,
    )

    assert [job.external_id for job in jobs] == ["fresh"]
    assert rejected == 1


def test_jobright_parser_keeps_jobright_as_the_primary_application_url() -> None:
    definition = FeedDefinition(
        "jobright_internships",
        "Jobright",
        "jobright.ai",
        "public",
        "software-engineer-intern-jobs-in-united-states",
        "internship",
        parse_jobright_next_data,
        fetch_url="https://jobright.ai/jobs/software-engineer-intern-jobs-in-united-states",
        provider="jobright",
    )
    jobs, rejected = parse_jobright_public_cards(
        [
            _jobright_card(
                "direct-apply",
                original_url="https://jobs.example.com/roles/direct-apply?utm_source=jobright",
            ),
            _jobright_card("invalid-original", original_url="javascript:alert(1)"),
        ],
        definition,
    )

    assert rejected == 0
    assert jobs[0].application_url == "https://jobright.ai/jobs/info/direct-apply"
    assert jobs[1].application_url == "https://jobright.ai/jobs/info/invalid-original"


def _jobright_definition(track: str = "internship") -> FeedDefinition:
    source_id = "jobright_internships" if track == "internship" else "jobright_new_grad"
    path = (
        "software-engineer-intern-jobs-in-united-states"
        if track == "internship"
        else "software-engineer-new-graduate-jobs-in-united-states"
    )
    return FeedDefinition(
        source_id,
        "Jobright",
        "jobright.ai",
        "public",
        path,
        track,
        parse_jobright_next_data,
        fetch_url=f"https://jobright.ai/jobs/{path}",
        provider="jobright",
        poll_interval_seconds=JOBRIGHT_POLL_INTERVAL_SECONDS,
    )


@pytest.mark.asyncio
async def test_jobright_fetcher_requests_the_computer_science_taxonomy_window() -> None:
    definition = _jobright_definition()

    class FakeHttpClient:
        def __init__(self) -> None:
            self.requests: list[str] = []
            self.bodies: list[dict] = []
            self.listing_fetches = 0

        async def get_text(self, _url, *, headers):
            self.listing_fetches += 1
            return _jobright_html({}, [_jobright_card("listing-only")]), {}, 200

        async def post_json(self, url, *, json):
            self.requests.append(url)
            self.bodies.append(json)
            cards = [_jobright_card("intern-1"), _jobright_card("intern-2")]
            return {"success": True, "result": {"jobList": cards}}, {}, 200

    client = FakeHttpClient()
    result = await GitHubFeedFetcher(jobright_http_client=client).fetch(  # type: ignore[arg-type]
        definition,
        _UnusedHttpClient(),  # type: ignore[arg-type]
        etag='"old-page"',
        last_modified="Wed, 12 Aug 2026 12:00:00 GMT",
    )

    # The listing page is a fallback surface only; a healthy poll never fetches it.
    assert client.listing_fetches == 0
    assert len(client.requests) == 1
    assert f"count={JOBRIGHT_PUBLIC_WINDOW_SIZE}" in client.requests[0]
    assert "sortCondition=1" in client.requests[0]

    body = client.bodies[0]
    assert [entry["taxonomyId"] for entry in body["jobTaxonomyList"]] == list(
        JOBRIGHT_TAXONOMY_IDS
    )
    assert body["seniority"] == [1]
    assert [job.external_id for job in result.observations] == ["intern-1", "intern-2"]
    assert result.degraded_reason is None


@pytest.mark.asyncio
async def test_jobright_fetcher_shares_one_window_across_both_tracks() -> None:
    class CountingHttpClient:
        def __init__(self) -> None:
            self.post_count = 0

        async def get_text(self, _url, *, headers):
            raise AssertionError("the listing page must not be fetched on a healthy poll")

        async def post_json(self, _url, *, json):
            self.post_count += 1
            cards = [
                _jobright_card("intern-1"),
                _jobright_card(
                    "grad-1",
                    title="Software Engineer I - Platform",
                    seniority="Entry Level",
                    employment_type="Full-time",
                ),
            ]
            return {"success": True, "result": {"jobList": cards}}, {}, 200

    client = CountingHttpClient()
    fetcher = GitHubFeedFetcher(jobright_http_client=client)  # type: ignore[arg-type]
    internships, new_grads = await asyncio.gather(
        fetcher.fetch(_jobright_definition("internship"), _UnusedHttpClient()),  # type: ignore[arg-type]
        fetcher.fetch(_jobright_definition("new_grad"), _UnusedHttpClient()),  # type: ignore[arg-type]
    )

    assert client.post_count == 1
    assert [job.external_id for job in internships.observations] == ["intern-1"]
    assert [job.external_id for job in new_grads.observations] == ["grad-1"]


@pytest.mark.asyncio
async def test_jobright_fetcher_marks_the_listing_fallback_as_degraded() -> None:
    definition = _jobright_definition()

    class BlockedWindowHttpClient:
        async def get_text(self, _url, *, headers):
            return _jobright_html({}, [_jobright_card("intern-1")]), {}, 200

        async def post_json(self, _url, *, json):
            raise RuntimeError(
                "POST https://jobright.ai/swan/recommend/visitor-list/jobs returned 403"
            )

    fetcher = GitHubFeedFetcher(jobright_http_client=BlockedWindowHttpClient())  # type: ignore[arg-type]
    result = await fetcher.fetch(
        definition,
        _UnusedHttpClient(),  # type: ignore[arg-type]
    )

    assert [job.external_id for job in result.observations] == ["intern-1"]
    assert result.rejected == 0
    # A truncated listing snapshot stays usable, but never reports itself as healthy.
    assert result.degraded_reason == "blocked"


@pytest.mark.asyncio
async def test_jobright_fetcher_stops_contacting_a_refusing_source() -> None:
    definition = _jobright_definition()

    class BlockedWindowHttpClient:
        def __init__(self) -> None:
            self.post_attempts = 0

        async def get_text(self, _url, *, headers):
            return _jobright_html({}, [_jobright_card("intern-1")]), {}, 200

        async def post_json(self, _url, *, json):
            self.post_attempts += 1
            raise TimeoutError("connection timed out")

    client = BlockedWindowHttpClient()
    fetcher = GitHubFeedFetcher(jobright_http_client=client)  # type: ignore[arg-type]
    assert fetcher.jobright_seconds_until_retry() == 0

    first = await fetcher.fetch(definition, _UnusedHttpClient())  # type: ignore[arg-type]

    assert first.degraded_reason == "timeout"
    assert client.post_attempts == 1
    assert fetcher.jobright_seconds_until_retry() > 0

    # The next poll must not add traffic to a door Jobright has already closed.
    second = await fetcher.fetch(definition, _UnusedHttpClient())  # type: ignore[arg-type]

    assert second.skipped is True
    assert second.degraded_reason == "backoff"
    assert second.observations == []
    assert client.post_attempts == 1


class _UnusedHttpClient:
    """Stands in for the pipeline's shared aiohttp client on a Jobright poll.

    Jobright's requests leave through the fetcher's own Chrome-impersonating client,
    so touching this one at all is the bug worth failing on.
    """

    def __getattr__(self, name: str):
        raise AssertionError(f"Jobright must not use the shared HTTP client ({name})")


class _RecordingJobrightClient:
    """Answers the visitor window per egress address, recording what it was sent."""

    def __init__(self, *, blocked_proxies: tuple[str, ...] = ()) -> None:
        self.blocked_proxies = blocked_proxies
        self.window_calls: list[dict[str, object]] = []
        self.listing_calls: list[dict[str, object]] = []

    async def get_text(self, _url, *, headers, **kwargs):
        self.listing_calls.append(kwargs)
        return _jobright_html({}, [_jobright_card("listing-1")]), {}, 200

    async def post_json(self, _url, *, json, **kwargs):
        self.window_calls.append(kwargs)
        proxy = str(kwargs.get("proxy", ""))
        if any(blocked in proxy for blocked in self.blocked_proxies):
            raise RuntimeError(
                "POST https://jobright.ai/swan/recommend/visitor-list/jobs returned 403"
            )
        return {"success": True, "result": {"jobList": [_jobright_card("intern-1")]}}, {}, 200


def _proxy(host: str, *, credentials: bool = False) -> ProxyEndpoint:
    return ProxyEndpoint(
        url=f"http://{host}:8080",
        username="isp-user" if credentials else None,
        password="isp-secret" if credentials else None,
    )


class _RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> bool:
        self.messages.append(message)
        return True


@pytest.mark.asyncio
async def test_jobright_announces_boot_rotation_degradation_and_recovery() -> None:
    definition = _jobright_definition()
    notifier = _RecordingNotifier()
    client = _RecordingJobrightClient()
    fetcher = GitHubFeedFetcher(
        jobright_proxies=[_proxy("isp-a.example.com"), _proxy("isp-b.example.com")],
        jobright_notifier=notifier,  # type: ignore[arg-type]
        jobright_http_client=client,  # type: ignore[arg-type]
    )

    await fetcher.fetch(definition, _UnusedHttpClient())  # type: ignore[arg-type]
    assert "Jobright online" in notifier.messages[0]
    assert "isp-a.example.com:8080" in notifier.messages[0]

    # Both addresses start refusing: one rotation, then the degraded announcement.
    client.blocked_proxies = ("isp-a.example.com", "isp-b.example.com")
    fetcher._jobright_window_cards = None
    await fetcher.fetch(definition, _UnusedHttpClient())  # type: ignore[arg-type]

    assert "rotated egress" in notifier.messages[1]
    assert "blocked (HTTP 403) on isp-a.example.com:8080" in notifier.messages[1]
    assert "retrying via isp-b.example.com:8080" in notifier.messages[1]
    # One rotation for two addresses: the exhausted final attempt does not announce
    # a retry it will not make, and reports the address it actually used.
    assert len(notifier.messages) == 3
    assert "Jobright degraded" in notifier.messages[2]
    assert "blocked (HTTP 403) via egress isp-b.example.com:8080" in notifier.messages[2]
    assert f"next attempt in {_format_duration(JOBRIGHT_BLOCK_BACKOFF_STEPS[0])}" in notifier.messages[2]

    # And the all-clear once a window succeeds again.
    client.blocked_proxies = ()
    fetcher._jobright_retry_after = 0.0
    fetcher._jobright_window_cards = None
    await fetcher.fetch(definition, _UnusedHttpClient())  # type: ignore[arg-type]

    assert "Jobright recovered" in notifier.messages[-1]
    assert "1 failed attempt" in notifier.messages[-1]
    assert "isp-b.example.com:8080" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_jobright_reads_its_proxy_pool_from_the_configured_list_file(
    tmp_path, monkeypatch
) -> None:
    listing = tmp_path / "jobright-proxies.txt"
    listing.write_text(
        "# provider export\n198.51.100.10:8080:isp-user:isp-pass\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JOBRIGHT_PROXY_FILE", str(listing))
    client = _RecordingJobrightClient()

    fetcher = GitHubFeedFetcher(jobright_http_client=client)  # type: ignore[arg-type]
    result = await fetcher.fetch(_jobright_definition(), _UnusedHttpClient())  # type: ignore[arg-type]

    assert result.degraded_reason is None
    assert fetcher.jobright_egress_label() == "198.51.100.10:8080"
    assert client.window_calls[0]["proxy"] == "http://198.51.100.10:8080"
    assert client.window_calls[0]["proxy_auth"] == ("isp-user", "isp-pass")


@pytest.mark.asyncio
async def test_jobright_window_leaves_through_the_configured_isp_proxy() -> None:
    client = _RecordingJobrightClient()
    fetcher = GitHubFeedFetcher(
        jobright_proxies=[_proxy("isp-a.example.com", credentials=True)],
        jobright_http_client=client,  # type: ignore[arg-type]
    )

    result = await fetcher.fetch(_jobright_definition(), _UnusedHttpClient())  # type: ignore[arg-type]

    assert result.degraded_reason is None
    assert [job.external_id for job in result.observations] == ["intern-1"]
    assert client.window_calls[0]["proxy"] == "http://isp-a.example.com:8080"
    assert client.window_calls[0]["proxy_auth"] == ("isp-user", "isp-secret")
    assert fetcher.jobright_egress_label() == "isp-a.example.com:8080"


@pytest.mark.asyncio
async def test_jobright_requests_stay_direct_when_no_proxy_is_configured() -> None:
    client = _RecordingJobrightClient()

    result = await GitHubFeedFetcher(jobright_http_client=client).fetch(  # type: ignore[arg-type]
        _jobright_definition(),
        _UnusedHttpClient(),  # type: ignore[arg-type]
    )

    assert result.degraded_reason is None
    # No proxy configured must mean no proxy keywords at all, not an empty proxy.
    assert client.window_calls == [{}]


@pytest.mark.asyncio
async def test_jobright_rotates_to_the_next_address_when_one_is_blocked() -> None:
    client = _RecordingJobrightClient(blocked_proxies=("isp-a.example.com",))
    fetcher = GitHubFeedFetcher(
        jobright_proxies=[_proxy("isp-a.example.com"), _proxy("isp-b.example.com")],
        jobright_http_client=client,  # type: ignore[arg-type]
    )

    result = await fetcher.fetch(_jobright_definition(), _UnusedHttpClient())  # type: ignore[arg-type]

    # A blocked address costs one retry, not the poll: the window still arrives whole.
    assert result.degraded_reason is None
    assert [job.external_id for job in result.observations] == ["intern-1"]
    assert [call["proxy"] for call in client.window_calls] == [
        "http://isp-a.example.com:8080",
        "http://isp-b.example.com:8080",
    ]
    assert client.listing_calls == []
    assert fetcher.jobright_seconds_until_retry() == 0
    # The working address is kept for the next poll rather than cycling every time.
    assert fetcher.jobright_egress_label() == "isp-b.example.com:8080"


@pytest.mark.asyncio
async def test_jobright_backs_off_once_every_address_is_refused() -> None:
    client = _RecordingJobrightClient(
        blocked_proxies=("isp-a.example.com", "isp-b.example.com")
    )
    fetcher = GitHubFeedFetcher(
        jobright_proxies=[_proxy("isp-a.example.com"), _proxy("isp-b.example.com")],
        jobright_http_client=client,  # type: ignore[arg-type]
    )

    result = await fetcher.fetch(_jobright_definition(), _UnusedHttpClient())  # type: ignore[arg-type]

    assert len(client.window_calls) == 2
    assert result.degraded_reason == "blocked"
    assert [job.external_id for job in result.observations] == ["listing-1"]
    # The snapshot leaves through the address of the last attempt, not a fresh one.
    assert client.listing_calls[0]["proxy"] == "http://isp-b.example.com:8080"
    assert fetcher.jobright_seconds_until_retry() > 0


@pytest.mark.asyncio
async def test_jobright_caps_how_many_addresses_one_poll_may_try() -> None:
    hosts = tuple(f"isp-{index}.example.com" for index in range(10))
    client = _RecordingJobrightClient(blocked_proxies=hosts)
    fetcher = GitHubFeedFetcher(
        jobright_proxies=[_proxy(host) for host in hosts],
        jobright_http_client=client,  # type: ignore[arg-type]
    )

    result = await fetcher.fetch(_jobright_definition(), _UnusedHttpClient())  # type: ignore[arg-type]

    # A large pool must not turn one poll into a burst across every address it owns.
    assert len(client.window_calls) == JOBRIGHT_MAX_EGRESS_ATTEMPTS
    assert result.degraded_reason == "blocked"
    assert fetcher.jobright_seconds_until_retry() > 0
    # The next poll resumes further along the pool instead of re-testing the head.
    assert fetcher.jobright_egress_label() == "isp-2.example.com:8080"


@pytest.mark.asyncio
async def test_jobright_does_not_rotate_addresses_on_a_query_scoped_failure() -> None:
    class MalformedWindowClient(_RecordingJobrightClient):
        async def post_json(self, _url, *, json, **kwargs):
            self.window_calls.append(kwargs)
            return {"success": False}, {}, 200

    client = MalformedWindowClient()
    fetcher = GitHubFeedFetcher(
        jobright_proxies=[_proxy("isp-a.example.com"), _proxy("isp-b.example.com")],
        jobright_http_client=client,  # type: ignore[arg-type]
    )

    result = await fetcher.fetch(_jobright_definition(), _UnusedHttpClient())  # type: ignore[arg-type]

    # An invalid payload repeats identically from every address; burning the pool on
    # it would hide the real cause and spend addresses for nothing.
    assert len(client.window_calls) == 1
    assert result.degraded_reason == "malformed"
    assert fetcher.jobright_egress_label() == "isp-a.example.com:8080"


@pytest.mark.asyncio
async def test_jobright_backoff_clears_after_a_successful_window() -> None:
    definition = _jobright_definition()

    class HealthyHttpClient:
        async def post_json(self, _url, *, json):
            return (
                {"success": True, "result": {"jobList": [_jobright_card("intern-1")]}},
                {},
                200,
            )

    fetcher = GitHubFeedFetcher(jobright_http_client=HealthyHttpClient())  # type: ignore[arg-type]
    fetcher._jobright_consecutive_failures = 3
    fetcher._jobright_retry_after = 0.0

    result = await fetcher.fetch(definition, _UnusedHttpClient())  # type: ignore[arg-type]

    assert result.degraded_reason is None
    assert fetcher.jobright_seconds_until_retry() == 0
    assert fetcher._jobright_consecutive_failures == 0


@pytest.mark.asyncio
async def test_jobright_fetcher_treats_an_empty_window_as_unavailable() -> None:
    definition = _jobright_definition()

    class EmptyWindowHttpClient:
        async def get_text(self, _url, *, headers):
            return _jobright_html({}, [_jobright_card("intern-1")]), {}, 200

        async def post_json(self, _url, *, json):
            return {"success": True, "result": {"jobList": []}}, {}, 200

    result = await GitHubFeedFetcher(jobright_http_client=EmptyWindowHttpClient()).fetch(  # type: ignore[arg-type]
        definition,
        _UnusedHttpClient(),  # type: ignore[arg-type]
    )

    # An empty window is a failed source, not evidence that every role disappeared.
    assert result.degraded_reason == "unavailable"
    assert [job.external_id for job in result.observations] == ["intern-1"]


def test_classify_jobright_failure_reads_a_wrapped_proxy_connection_error() -> None:
    # aiohttp phrases a dead proxy as "Cannot connect to host", identical to a dead
    # upstream; only the chained exception type separates the two.
    cause = aiohttp.ClientProxyConnectionError(
        aiohttp.client_reqrep.ConnectionKey(
            "isp-a.example.com", 8080, False, True, None, None, None
        ),
        OSError("Connection refused"),
    )
    wrapped = RuntimeError("HTTP request failed for https://jobright.ai: cannot connect")
    wrapped.__cause__ = cause

    assert classify_jobright_failure(wrapped) == "proxy_error"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RuntimeError("GET https://jobright.ai returned 403: forbidden"), "blocked"),
        (RuntimeError('{"errorCode":43004}'), "blocked"),
        (
            RuntimeError("HTTP request failed: Cannot connect to proxy isp-a:8080"),
            "proxy_error",
        ),
        (RuntimeError("POST https://jobright.ai returned 407: auth required"), "proxy_error"),
        (RuntimeError("POST https://jobright.ai returned 429"), "throttled"),
        (RuntimeError("POST https://jobright.ai returned 503"), "upstream_error"),
        (TimeoutError("connection timed out"), "timeout"),
        (RuntimeError("visitor API returned an invalid payload"), "malformed"),
        (RuntimeError("something else entirely"), "unavailable"),
    ],
)
def test_classify_jobright_failure_names_the_cause(error: BaseException, expected: str) -> None:
    assert classify_jobright_failure(error) == expected


def test_jobright_backoff_escalates_then_caps() -> None:
    steps = [jobright_backoff_seconds(n) for n in range(1, len(JOBRIGHT_BLOCK_BACKOFF_STEPS) + 2)]

    assert steps[0] == JOBRIGHT_BLOCK_BACKOFF_STEPS[0]
    assert steps == sorted(steps)
    assert steps[-1] == JOBRIGHT_BLOCK_BACKOFF_STEPS[-1]
    assert jobright_backoff_seconds(0) == JOBRIGHT_POLL_INTERVAL_SECONDS


def test_jobright_taxonomy_ids_are_unique_and_computer_science_scoped() -> None:
    assert len(JOBRIGHT_TAXONOMY_IDS) == len(set(JOBRIGHT_TAXONOMY_IDS))
    # Group 01 is Jobright's software/internet/AI family; 02-01-03 is embedded software.
    assert all(
        taxonomy_id.startswith("01-") or taxonomy_id == "02-01-03"
        for taxonomy_id in JOBRIGHT_TAXONOMY_IDS
    )
    # The listing pages only ever carry the fallback wildcard, which selects nothing
    # specific and is what limited the source to one broad slice.
    assert "00-00-00" not in JOBRIGHT_TAXONOMY_IDS


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


def test_jobright_query_never_sends_an_empty_taxonomy_title() -> None:
    # An empty title returns zero rows with success:true and no error code, which is
    # indistinguishable from a genuinely empty window. Only a non-empty title works.
    query = build_jobright_public_query()

    assert query["jobTaxonomyList"], "the endpoint rejects an empty taxonomy list"
    for entry in query["jobTaxonomyList"]:
        assert entry["taxonomyId"] in JOBRIGHT_TAXONOMY_IDS
        assert entry["title"], f"{entry['taxonomyId']} would send an empty title"


def test_jobright_window_with_no_matching_track_is_healthy_not_failed() -> None:
    """One shared window splits into two tracks, so either side can come up empty.

    Jobright indexes in batches, so a window of thirty consecutive internships is an
    ordinary morning. Raising there would mark the feed failed and block the run's
    baseline over a perfectly good fetch.
    """
    internship_definition = _jobright_definition("internship")
    new_grad_definition = _jobright_definition("new_grad")
    cards = [_jobright_card(f"intern-{index}") for index in range(5)]

    internships, _ = parse_jobright_public_cards(cards, internship_definition)
    new_grads, rejected = parse_jobright_public_cards(cards, new_grad_definition)

    assert [job.external_id for job in internships] == [f"intern-{i}" for i in range(5)]
    assert new_grads == []
    assert rejected == 5


def test_jobright_empty_window_is_still_a_failed_source() -> None:
    # An empty window means the API returned nothing, which is not the same as a
    # window that held nothing for this track. That guard must not be weakened.
    with pytest.raises(ValueError, match="produced no public job cards"):
        parse_jobright_public_cards([], _jobright_definition("internship"))
