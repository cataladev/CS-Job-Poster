from __future__ import annotations

import pytest

from job_bot.github_feeds import JobObservation
from job_bot.jobright_links import (
    FAILURE_STREAK_LIMIT,
    JobrightLinkResolver,
    _extract_original_url,
)


def _observation(job_id: str = "jr-1") -> JobObservation:
    return JobObservation(
        source_id="jobright_internships",
        source_label="Jobright",
        source_url="https://jobright.ai",
        external_id=job_id,
        company="Example Corp",
        title="Software Engineer Intern",
        locations=["Austin, TX"],
        application_url=f"https://jobright.ai/jobs/info/{job_id}",
        track="internship",
    )


def _payload(url: str | None, *, logined: bool = True) -> dict:
    job: dict = {"jobId": "x", "jobTitle": "Software Engineer Intern"}
    if url is not None:
        job["originalUrl"] = url
    return {"pageProps": {"logined": logined, "dataSource": {"jobResult": job}}}


class FakeHttpClient:
    """Serves the build id page, then one detail payload per job id."""

    def __init__(self, payloads: dict[str, object], *, build_id: str = "BUILD1") -> None:
        self.payloads = payloads
        self.build_id = build_id
        self.detail_requests: list[str] = []
        self.build_id_requests = 0

    async def get_text(self, _url, *, headers, **_kw):
        self.build_id_requests += 1
        return f'<script>{{"buildId":"{self.build_id}"}}</script>', {}, 200

    async def get_json(self, url, *, headers, **_kw):
        assert headers.get("Cookie", "").startswith("SESSION_ID="), "cookie must be sent"
        job_id = url.rsplit("/", 1)[-1].removesuffix(".json")
        self.detail_requests.append(job_id)
        value = self.payloads[job_id]
        if isinstance(value, Exception):
            raise value
        return value, {}, 200


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> bool:
        self.messages.append(message)
        return True


def test_extract_reads_the_employer_url_and_session_state() -> None:
    url, session = _extract_original_url(_payload("https://boards.example.com/jobs/1"))
    assert url == "https://boards.example.com/jobs/1"
    assert session == "authenticated"

    # A signed-out render is the one thing that proves the cookie is not working.
    assert _extract_original_url(_payload("https://x.example.com/1", logined=False))[1] == "anonymous"

    # Anything unparseable proves nothing either way and must not be read as proof.
    for unreadable in (None, "", {}, {"pageProps": {}}, {"pageProps": {"dataSource": {}}}):
        assert _extract_original_url(unreadable)[1] == "unknown"


def test_extract_ignores_a_jobright_url_echoed_back() -> None:
    payload = _payload("https://jobright.ai/jobs/info/abc123")
    url, session = _extract_original_url(payload)

    # Jobright's own wrapper is what we already have; it is not a resolution.
    assert url == ""
    assert session == "authenticated"


@pytest.mark.asyncio
async def test_disabled_without_a_session_leaves_links_untouched() -> None:
    resolver = JobrightLinkResolver("")
    client = FakeHttpClient({})
    observations = [_observation()]

    assert resolver.enabled is False
    assert await resolver.resolve(client, observations) == observations
    assert client.detail_requests == []


@pytest.mark.asyncio
async def test_resolved_link_keeps_jobright_as_the_fallback() -> None:
    resolver = JobrightLinkResolver("cookie")
    client = FakeHttpClient({"jr-1": _payload("https://boards.example.com/jobs/1")})

    [job] = await resolver.resolve(client, [_observation()])

    assert job.application_url == "https://boards.example.com/jobs/1"
    # The publisher renders both links from this key.
    assert job.raw_data["jobright_fallback_url"] == "https://jobright.ai/jobs/info/jr-1"
    assert job.raw_data["jobright_link_resolved"] is True


@pytest.mark.asyncio
async def test_each_job_is_resolved_once_across_polls() -> None:
    resolver = JobrightLinkResolver("cookie")
    client = FakeHttpClient({"jr-1": _payload("https://boards.example.com/jobs/1")})

    for _ in range(4):
        await resolver.resolve(client, [_observation()])

    # The window holds the same roles for hours; re-resolving them every minute is
    # what would turn this into thousands of authenticated requests a day.
    assert client.detail_requests == ["jr-1"]
    assert client.build_id_requests == 1


@pytest.mark.asyncio
async def test_a_job_with_no_employer_url_is_not_retried() -> None:
    resolver = JobrightLinkResolver("cookie")
    client = FakeHttpClient({"jr-1": _payload(None)})

    for _ in range(3):
        [job] = await resolver.resolve(client, [_observation()])

    assert job.application_url == "https://jobright.ai/jobs/info/jr-1"
    assert "jobright_link_resolved" not in job.raw_data
    # A signed-in "there is no employer URL" is a real answer worth remembering.
    assert client.detail_requests == ["jr-1"]


@pytest.mark.asyncio
async def test_a_transient_failure_is_retried_on_the_next_poll() -> None:
    resolver = JobrightLinkResolver("cookie")
    client = FakeHttpClient({"jr-1": RuntimeError("GET ... returned 502")})

    [first] = await resolver.resolve(client, [_observation()])
    assert first.application_url == "https://jobright.ai/jobs/info/jr-1"

    client.payloads["jr-1"] = _payload("https://boards.example.com/jobs/1")
    [second] = await resolver.resolve(client, [_observation()])

    # Caching the failure would have frozen the Jobright link in place forever.
    assert second.application_url == "https://boards.example.com/jobs/1"
    assert client.detail_requests == ["jr-1", "jr-1"]


@pytest.mark.asyncio
async def test_one_anonymous_response_does_not_declare_the_session_dead() -> None:
    notifier = RecordingNotifier()
    resolver = JobrightLinkResolver("cookie", notifier=notifier)
    client = FakeHttpClient(
        {
            "jr-1": _payload("https://boards.example.com/1", logined=False),
            "jr-2": _payload("https://boards.example.com/2"),
        }
    )

    await resolver.resolve(client, [_observation("jr-1"), _observation("jr-2")])

    # Jobright serves the occasional signed-out render to a perfectly good session.
    assert notifier.messages == []
    assert resolver.suspended is False


@pytest.mark.asyncio
async def test_a_dead_session_is_reported_once_then_stops_resolving() -> None:
    notifier = RecordingNotifier()
    resolver = JobrightLinkResolver("cookie", notifier=notifier)
    job_ids = [f"jr-{index}" for index in range(FAILURE_STREAK_LIMIT + 2)]
    client = FakeHttpClient({job_id: _payload("https://x.example.com", logined=False) for job_id in job_ids})

    observations = [_observation(job_id) for job_id in job_ids]
    unchanged = await resolver.resolve(client, observations)

    assert unchanged == observations
    assert resolver.suspended is True
    assert len(notifier.messages) == 1
    assert "session expired" in notifier.messages[0]

    requests_so_far = len(client.detail_requests)
    await resolver.resolve(client, observations)

    # A dead cookie must not keep costing an authenticated request per job per poll.
    assert client.detail_requests == client.detail_requests[:requests_so_far]
    assert len(notifier.messages) == 1


@pytest.mark.asyncio
async def test_a_stale_build_id_is_reloaded_after_a_404() -> None:
    resolver = JobrightLinkResolver("cookie")
    client = FakeHttpClient({"jr-1": RuntimeError("GET ... returned 404: <html>")})

    await resolver.resolve(client, [_observation()])
    assert client.build_id_requests == 1

    client.payloads["jr-1"] = _payload("https://boards.example.com/jobs/1")
    [job] = await resolver.resolve(client, [_observation()])

    # Jobright rotates the deploy id on every release; the route 404s until reloaded.
    assert client.build_id_requests == 2
    assert job.application_url == "https://boards.example.com/jobs/1"
