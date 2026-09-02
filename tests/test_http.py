from __future__ import annotations

from types import SimpleNamespace

import base64

import pytest

from job_bot.utils.http import (
    AsyncHttpClient,
    describe_proxy_pool,
    load_proxy_pool,
    parse_proxy_endpoint,
)


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


def test_proxy_endpoint_keeps_credentials_out_of_the_url_and_label() -> None:
    endpoint = parse_proxy_endpoint("http://user:secret@isp.example.com:8080")

    assert endpoint.url == "http://isp.example.com:8080"
    assert endpoint.label == "isp.example.com:8080"
    assert "secret" not in repr(endpoint.url)
    kwargs = endpoint.request_kwargs()
    assert kwargs["proxy"] == "http://isp.example.com:8080"
    expected = base64.b64encode(b"user:secret").decode("ascii")
    assert kwargs["proxy_headers"] == {"Proxy-Authorization": f"Basic {expected}"}


def test_proxy_pool_applies_shared_credentials_and_skips_malformed_entries() -> None:
    pool = load_proxy_pool(
        "JOBRIGHT_PROXY_URLS",
        username_env="JOBRIGHT_PROXY_USERNAME",
        password_env="JOBRIGHT_PROXY_PASSWORD",
        environ={
            "JOBRIGHT_PROXY_URLS": (
                "http://a.example.com:8080, not-a-url ,http://own:pass@b.example.com:9000"
            ),
            "JOBRIGHT_PROXY_USERNAME": "shared",
            "JOBRIGHT_PROXY_PASSWORD": "shared-secret",
        },
    )

    assert [endpoint.label for endpoint in pool] == ["a.example.com:8080", "b.example.com:9000"]
    assert pool[0].username == "shared"
    # A per-endpoint credential in the URL wins over the shared pair.
    assert pool[1].username == "own"
    assert describe_proxy_pool(pool) == "a.example.com:8080,b.example.com:9000"


def test_proxy_pool_is_empty_when_unconfigured() -> None:
    assert load_proxy_pool("JOBRIGHT_PROXY_URLS", environ={}) == ()
    assert describe_proxy_pool(()) == "direct"


def test_proxy_pool_reads_a_provider_export_file(tmp_path) -> None:
    listing = tmp_path / "jobright-proxies.txt"
    listing.write_text(
        "\n".join(
            [
                "# exported from the provider, one per line",
                "198.51.100.10:8080:file-user:file-pass",
                "",
                "file-user2:file-pass2:198.51.100.11:8080  # second address",
                "198.51.100.12:8080",
                "198.51.100.10:8080:file-user:file-pass",
            ]
        ),
        encoding="utf-8",
    )

    pool = load_proxy_pool(
        "JOBRIGHT_PROXY_URLS",
        file_env="JOBRIGHT_PROXY_FILE",
        username_env="JOBRIGHT_PROXY_USERNAME",
        password_env="JOBRIGHT_PROXY_PASSWORD",
        environ={
            "JOBRIGHT_PROXY_FILE": str(listing),
            "JOBRIGHT_PROXY_URLS": "http://198.51.100.13:8080",
            "JOBRIGHT_PROXY_USERNAME": "shared",
            "JOBRIGHT_PROXY_PASSWORD": "shared-secret",
        },
    )

    # File entries first, then the inline ones; the repeated address collapses.
    assert [endpoint.label for endpoint in pool] == [
        "198.51.100.10:8080",
        "198.51.100.11:8080",
        "198.51.100.12:8080",
        "198.51.100.13:8080",
    ]
    # Both provider field orders are read, and an entry without credentials of its
    # own falls back to the shared pair.
    assert [endpoint.username for endpoint in pool] == [
        "file-user",
        "file-user2",
        "shared",
        "shared",
    ]


def test_proxy_pool_survives_an_unreadable_list_file(tmp_path, caplog) -> None:
    with caplog.at_level("ERROR"):
        pool = load_proxy_pool(
            "JOBRIGHT_PROXY_URLS",
            file_env="JOBRIGHT_PROXY_FILE",
            environ={
                "JOBRIGHT_PROXY_FILE": str(tmp_path / "missing.txt"),
                "JOBRIGHT_PROXY_URLS": "http://198.51.100.20:8080",
            },
        )

    # A named-but-unreadable list is loud, but the runtime still starts.
    assert [endpoint.label for endpoint in pool] == ["198.51.100.20:8080"]
    assert "proxy_file_unreadable" in caplog.text
