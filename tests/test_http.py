from __future__ import annotations

from types import SimpleNamespace

import pytest

from job_bot.utils.http import AsyncHttpClient


class FakeResponse:
    def __init__(self, status: int, text: str = "") -> None:
        self.status = status
        self.body = text
        self.released = False

    def release(self) -> None:
        self.released = True

    async def text(self) -> str:
        return self.body


@pytest.mark.asyncio
async def test_transient_response_is_released_and_retried(monkeypatch) -> None:
    unavailable = FakeResponse(503)
    success = FakeResponse(200)
    responses = iter([unavailable, success])

    async def request(_method, _url, **_kwargs):
        return next(responses)

    async def no_sleep(_delay):
        return None

    client = AsyncHttpClient(timeout_seconds=5, retries=1, user_agent="test")
    client._session = SimpleNamespace(request=request)  # type: ignore[assignment]
    monkeypatch.setattr("job_bot.utils.http.asyncio.sleep", no_sleep)

    response = await client.request("GET", "https://example.com/feed")

    assert response is success
    assert unavailable.released


@pytest.mark.asyncio
async def test_non_retryable_response_fails_immediately() -> None:
    not_found = FakeResponse(404, "missing")
    calls = 0

    async def request(_method, _url, **_kwargs):
        nonlocal calls
        calls += 1
        return not_found

    client = AsyncHttpClient(timeout_seconds=5, retries=3, user_agent="test")
    client._session = SimpleNamespace(request=request)  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="returned 404: missing"):
        await client.request("GET", "https://example.com/feed")

    assert calls == 1
    assert not_found.released
