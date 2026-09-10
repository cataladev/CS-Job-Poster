from __future__ import annotations

from types import SimpleNamespace

import pytest

from curl_cffi import CurlError

from job_bot.utils.http import (
    AsyncHttpClient,
    CHROME_IMPERSONATION,
    ImpersonatingHttpClient,
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
    # curl authenticates the proxy itself, from the pair rather than from a header.
    assert kwargs["proxy_auth"] == ("user", "secret")


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


class FakeCurlResponse:
    def __init__(self, status_code: int, text: str = "", payload: object = None) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = {"Content-Type": "application/json"}
        self._payload = payload

    def json(self) -> object:
        return self._payload


class FakeCurlSession:
    """Records what curl_cffi would have been asked to send."""

    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[dict] = []
        self.closed = False

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        answer = self.responses.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def close(self) -> None:
        self.closed = True


def _impersonating(
    responses: list[object], *, retries: int = 1
) -> tuple[ImpersonatingHttpClient, FakeCurlSession]:
    client = ImpersonatingHttpClient(timeout_seconds=5, retries=retries)
    session = FakeCurlSession(responses)
    client._session = session  # type: ignore[assignment]
    return client, session


@pytest.mark.asyncio
async def test_impersonating_client_sends_the_proxy_and_the_configured_timeout() -> None:
    client, session = _impersonating([FakeCurlResponse(200, payload={"ok": True})])
    endpoint = parse_proxy_endpoint("http://user:secret@isp.example.com:8080")

    payload, headers, status = await client.post_json(
        "https://jobright.ai/swan/recommend/visitor-list/jobs",
        json={"query": 1},
        **endpoint.request_kwargs(),
    )

    assert (payload, status) == ({"ok": True}, 200)
    assert headers["Content-Type"] == "application/json"
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["json"] == {"query": 1}
    assert call["proxy"] == "http://isp.example.com:8080"
    assert call["proxy_auth"] == ("user", "secret")
    assert call["timeout"] == 5


def test_impersonating_client_opens_its_session_as_chrome() -> None:
    # The whole point of this client: the handshake has to be Chrome's, not curl's,
    # and a named build rather than the alias that tracks curl_cffi's newest profile.
    assert ImpersonatingHttpClient(timeout_seconds=5, retries=0)._impersonate == CHROME_IMPERSONATION
    assert CHROME_IMPERSONATION.startswith("chrome") and CHROME_IMPERSONATION != "chrome"


@pytest.mark.asyncio
async def test_impersonating_client_retries_a_transient_status_then_reports_it(
    monkeypatch,
) -> None:
    client, session = _impersonating(
        [FakeCurlResponse(503), FakeCurlResponse(200, payload={"ok": True})]
    )

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("job_bot.utils.http.asyncio.sleep", no_sleep)

    payload, _, _ = await client.get_json("https://jobright.ai/feed")

    assert payload == {"ok": True}
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_impersonating_client_reports_a_refused_status_the_way_callers_read_it() -> None:
    client, _ = _impersonating([FakeCurlResponse(403, text="Access denied")])

    # Jobright's failure classifier reads these strings, so their shape is load-bearing.
    with pytest.raises(RuntimeError, match="returned 403: Access denied"):
        await client.get_text("https://jobright.ai/feed")


@pytest.mark.asyncio
async def test_a_transport_failure_through_a_proxy_names_the_proxy() -> None:
    refused = CurlError("Failed to connect to isp.example.com port 8080", code=7)
    client, _ = _impersonating([refused], retries=0)

    with pytest.raises(RuntimeError) as raised:
        await client.get_text(
            "https://jobright.ai/feed", proxy="http://isp.example.com:8080"
        )

    # curl phrases a dead proxy exactly as it phrases a dead host; only the request
    # that used one can tell them apart, and the classifier needs the distinction.
    assert "cannot connect to proxy" in str(raised.value)


@pytest.mark.asyncio
async def test_a_direct_transport_failure_is_not_blamed_on_a_proxy() -> None:
    client, _ = _impersonating(
        [CurlError("Failed to connect to jobright.ai port 443", code=7)], retries=0
    )

    with pytest.raises(RuntimeError) as raised:
        await client.get_text("https://jobright.ai/feed")

    assert "cannot connect to proxy" not in str(raised.value)
    assert "HTTP request failed for https://jobright.ai/feed" in str(raised.value)
