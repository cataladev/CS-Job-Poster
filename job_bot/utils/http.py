from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence
from urllib.parse import unquote, urlsplit, urlunsplit

import aiohttp
from curl_cffi import CurlError
from curl_cffi.requests import AsyncSession, Response


@dataclass(frozen=True)
class ProxyEndpoint:
    """One egress address a request can leave through, such as an ISP proxy.

    Sources that throttle by source address see the proxy's address instead of the
    deployment's. Credentials are kept out of ``url`` so that no log line, error
    message, or repr can carry them.
    """

    url: str
    username: Optional[str] = None
    password: Optional[str] = None

    @property
    def label(self) -> str:
        """Host and port only -- safe to log."""
        split = urlsplit(self.url)
        if not split.hostname:
            return "proxy"
        return f"{split.hostname}:{split.port}" if split.port else split.hostname

    def request_kwargs(self) -> Dict[str, Any]:
        """Keyword arguments that route one request through this proxy.

        curl authenticates a proxy itself, from the pair handed to ``proxy_auth``,
        rather than from a header the caller assembles: for an HTTPS target the
        credentials belong on the ``CONNECT``, and only the client knows how to put
        them there. They stay out of ``url`` so no log line or repr can carry them.
        """
        kwargs: Dict[str, Any] = {"proxy": self.url}
        if self.username is not None:
            kwargs["proxy_auth"] = (self.username, self.password or "")
        return kwargs


def _normalize_proxy_url(raw: str) -> str:
    """Accept the shapes proxy providers actually hand out, not just full URLs.

    Recognized: ``scheme://user:pass@host:port``, ``user:pass@host:port``,
    ``host:port``, ``host:port:user:pass``, and ``user:pass:host:port``. The two
    four-field orders are told apart by which side carries the numeric port, which
    is the only thing that distinguishes them. A missing scheme becomes ``http``,
    the scheme a proxy speaks even when the target it tunnels to is HTTPS.
    """
    token = raw.strip()
    if "://" in token:
        return token
    if "@" in token:
        return f"http://{token}"
    fields = token.split(":")
    if len(fields) == 2:
        return f"http://{token}"
    if len(fields) == 4:
        if fields[1].isdigit():
            host, port, username, password = fields
        elif fields[3].isdigit():
            username, password, host, port = fields
        else:
            raise ValueError(f"invalid proxy entry: {raw!r}")
        return f"http://{username}:{password}@{host}:{port}"
    raise ValueError(f"invalid proxy entry: {raw!r}")


def parse_proxy_endpoint(
    raw_url: str,
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> ProxyEndpoint:
    """Build an endpoint from a proxy entry, lifting any inline credentials out of it.

    Any shape ``_normalize_proxy_url`` accepts works here. Inline credentials win over
    the shared username/password pair, because they are the per-endpoint ones.
    """
    split = urlsplit(_normalize_proxy_url(raw_url))
    if not split.scheme or not split.hostname:
        raise ValueError(f"invalid proxy entry: {raw_url!r}")
    netloc = split.hostname if split.port is None else f"{split.hostname}:{split.port}"
    return ProxyEndpoint(
        url=urlunsplit((split.scheme, netloc, split.path, split.query, split.fragment)),
        username=unquote(split.username) if split.username else username,
        password=unquote(split.password) if split.password else password,
    )


def read_proxy_list(
    text: str,
    *,
    username: Optional[str] = None,
    password: Optional[str] = None,
    source: str = "proxy list",
) -> tuple[ProxyEndpoint, ...]:
    """Parse a proxy list separated by newlines, commas, or both.

    Provider exports are usually one entry per line, so blank lines and ``#``
    comments are ignored and a malformed entry is skipped with a warning rather than
    taking the runtime down over one typo. Duplicates collapse, since a repeated
    address would otherwise be tried twice while a distinct one went unused.
    """
    endpoints: list[ProxyEndpoint] = []
    seen: set[str] = set()
    for line in text.splitlines():
        entry = line.split("#", 1)[0]
        for candidate in entry.split(","):
            token = candidate.strip()
            if not token:
                continue
            try:
                endpoint = parse_proxy_endpoint(
                    token, username=username, password=password
                )
            except ValueError:
                logging.getLogger(__name__).warning(
                    "proxy_entry_ignored source=%s reason=unrecognized_format", source
                )
                continue
            key = f"{endpoint.url}|{endpoint.username}"
            if key in seen:
                continue
            seen.add(key)
            endpoints.append(endpoint)
    return tuple(endpoints)


def load_proxy_pool(
    urls_env: str,
    *,
    file_env: Optional[str] = None,
    username_env: Optional[str] = None,
    password_env: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> tuple[ProxyEndpoint, ...]:
    """Read a proxy pool from the environment, inline or from a file.

    ``file_env`` names a path to a provider's exported list -- the practical way to
    carry more than a handful of addresses, and one that keeps credentials out of the
    process environment. Entries from the file come first, then any named inline;
    duplicates across the two collapse. An unset or empty configuration yields an
    empty pool, which callers read as "leave through the deployment's own address".
    """
    env = os.environ if environ is None else environ
    username = env.get(username_env) if username_env else None
    password = env.get(password_env) if password_env else None
    logger = logging.getLogger(__name__)

    sources: list[tuple[str, str]] = []
    path = (env.get(file_env, "") if file_env else "").strip()
    if path:
        try:
            sources.append((path, Path(path).expanduser().read_text(encoding="utf-8")))
        except OSError as exc:
            # A named-but-unreadable list is an operator error worth seeing, but it
            # must not stop the rest of the runtime from starting.
            logger.error("proxy_file_unreadable env=%s path=%s error=%s", file_env, path, exc)
    sources.append((urls_env, env.get(urls_env, "")))

    endpoints: list[ProxyEndpoint] = []
    seen: set[str] = set()
    for source, text in sources:
        for endpoint in read_proxy_list(
            text, username=username, password=password, source=source
        ):
            key = f"{endpoint.url}|{endpoint.username}"
            if key not in seen:
                seen.add(key)
                endpoints.append(endpoint)
    return tuple(endpoints)


def describe_proxy_pool(endpoints: Sequence[ProxyEndpoint]) -> str:
    """Render a pool for logs without exposing credentials."""
    return ",".join(endpoint.label for endpoint in endpoints) or "direct"


# Why a request failed, in the vocabulary the Jobright alerts and logs are written
# in. It lives here, beside the clients that raise these errors, so that every caller
# reads one failure the same way -- the feed fetcher and the link resolver included.
def classify_jobright_failure(error: BaseException) -> str:
    """Name why a visitor window failed so operators can act on the right cause.

    Jobright answers a throttled source address with ``403``/``errorCode 43004`` and
    an address it has stopped serving with a connection timeout. Both previously
    reached the logs as one undifferentiated warning, which is how a blocked
    deployment ran for weeks on a silently truncated snapshot.

    A configured proxy adds a third party that can fail. Its failures are named
    first, because an unreachable proxy is a broken egress rather than a slow
    Jobright, and the two call for different operator action. The HTTP client wraps
    transport errors in ``RuntimeError``, so the chained cause carries the only
    reliable evidence of which one it was.
    """
    chain = _exception_chain(error)
    text = " ".join(str(item) for item in chain).lower()
    if any(
        isinstance(item, (aiohttp.ClientProxyConnectionError, aiohttp.ClientHttpProxyError))
        for item in chain
    ) or "cannot connect to proxy" in text or "returned 407" in text:
        return "proxy_error"
    if (
        "timeout" in text
        or "timed out" in text
        or any(isinstance(item, TimeoutError) for item in chain)
    ):
        return "timeout"
    if "returned 403" in text or "43004" in text:
        return "blocked"
    if "returned 429" in text:
        return "throttled"
    if "returned 5" in text:
        return "upstream_error"
    if "invalid payload" in text:
        return "malformed"
    return "unavailable"


def _exception_chain(error: BaseException) -> list[BaseException]:
    """The error plus the causes it was raised from, without cycling."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def extract_http_status(error: BaseException | str) -> int | None:
    """Recover the HTTP status from a failure, when the request got one at all.

    Transport-level failures -- a timeout, a TLS handshake, a refused proxy connect --
    never receive a response, so they have no status to report. Returning ``None``
    for those keeps an alert honest instead of inventing a code.
    """
    match = re.search(r"returned (\d{3})\b", str(error))
    return int(match.group(1)) if match else None


class AsyncHttpClient:
    def __init__(
        self,
        timeout_seconds: int,
        retries: int,
        user_agent: str,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._retries = retries
        self._user_agent = user_agent
        self._session: Optional[aiohttp.ClientSession] = None
        self._logger = logging.getLogger(self.__class__.__name__)

    async def __aenter__(self) -> "AsyncHttpClient":
        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": self._user_agent},
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session:
            await self._session.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if not self._session:
            raise RuntimeError("HTTP client session is not initialized")
        return self._session

    async def request(
        self,
        method: str,
        url: str,
        *,
        expected_status: tuple[int, ...] = (200,),
        **kwargs: Any,
    ) -> aiohttp.ClientResponse:
        attempt = 0
        last_error: Optional[Exception] = None
        while attempt <= self._retries:
            try:
                response = await self.session.request(method, url, **kwargs)
                if response.status in expected_status:
                    return response
                if response.status in {429, 500, 502, 503, 504}:
                    response.release()
                    last_error = RuntimeError(f"{method} {url} returned {response.status}")
                    if attempt >= self._retries:
                        break
                    await asyncio.sleep(min(2 ** attempt, 8))
                    attempt += 1
                    continue
                text = await response.text()
                response.release()
                raise RuntimeError(f"{method} {url} returned {response.status}: {text[:200]}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt >= self._retries:
                    break
                await asyncio.sleep(min(2 ** attempt, 8))
                attempt += 1
        # Chained, so callers can still see the original aiohttp error type -- a proxy
        # connection failure and an unreachable host read identically as text.
        raise RuntimeError(f"HTTP request failed for {url}: {last_error}") from last_error

    async def get_text(self, url: str, **kwargs: Any) -> tuple[str, Dict[str, str], int]:
        response = await self.request("GET", url, expected_status=(200, 304), **kwargs)
        async with response:
            text = await response.text()
            return text, dict(response.headers), response.status

    async def get_json(self, url: str, **kwargs: Any) -> tuple[Any, Dict[str, str], int]:
        response = await self.request("GET", url, expected_status=(200, 304), **kwargs)
        async with response:
            data = await response.json(content_type=None)
            return data, dict(response.headers), response.status

    async def post_json(self, url: str, **kwargs: Any) -> tuple[Any, Dict[str, str], int]:
        response = await self.request("POST", url, expected_status=(200, 201, 304), **kwargs)
        async with response:
            if response.status == 304:
                return None, dict(response.headers), response.status
            data = await response.json(content_type=None)
            return data, dict(response.headers), response.status


# Jobright's edge screens the TLS and HTTP/2 handshake before it reads a single
# header, so a request that only claims to be Chrome in its User-Agent is refused
# no matter which address it leaves through. curl_cffi replays Chrome's actual
# fingerprint.
#
# Pinned to a specific build rather than curl_cffi's unversioned ``chrome`` alias:
# the alias tracks the newest profile it ships, and that profile is currently newer
# than Jobright's edge has learned to recognize -- it answers it with a security
# interstitial while every named build from chrome110 through chrome142 is served the
# real page. Moving this forward is safe only against a live check.
CHROME_IMPERSONATION = "chrome142"

# curl reports a refused proxy exactly as it reports a refused host, so only the
# knowledge that the attempt used one tells them apart: unresolved proxy name,
# refused connection, dropped tunnel, and a proxy handshake that failed outright.
# The failure classifier reads these strings, so a proxy failure has to name itself.
_PROXY_TRANSPORT_CURL_CODES = frozenset({5, 7, 56, 97})


def _proxy_aware_error(error: BaseException, *, via_proxy: bool) -> BaseException:
    """Name a transport failure that belongs to the proxy rather than the host."""
    if via_proxy and getattr(error, "code", None) in _PROXY_TRANSPORT_CURL_CODES:
        return RuntimeError(f"cannot connect to proxy: {error}")
    return error


class ImpersonatingHttpClient:
    """An HTTP client that presents itself as Chrome, down to the TLS handshake.

    The surface matches :class:`AsyncHttpClient` -- the same ``get_text``,
    ``get_json``, ``post_json``, the same retry policy, and the same error strings --
    so a caller moved onto it reads its failures exactly as before. What differs is
    the wire: aiohttp's handshake is recognizably not a browser's, which is what a
    fingerprinting edge refuses before any header or address is considered.

    A proxy is passed per request rather than held on the session, because the pool
    rotates addresses between calls while the session lives for the whole process.
    """

    def __init__(
        self,
        timeout_seconds: int,
        retries: int,
        impersonate: str = CHROME_IMPERSONATION,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._retries = retries
        self._impersonate = impersonate
        self._session: Optional[AsyncSession] = None
        self._session_lock = asyncio.Lock()

    async def __aenter__(self) -> "ImpersonatingHttpClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def _ensure_session(self) -> AsyncSession:
        """Open the shared session on first use.

        Callers that own this client for the life of the process never enter it as a
        context manager, so the session cannot be built in ``__aenter__`` alone.
        """
        async with self._session_lock:
            if self._session is None:
                self._session = AsyncSession(
                    impersonate=self._impersonate,
                    # Proxies come from the configured pool and nowhere else. curl
                    # would otherwise pick up ambient ``HTTPS_PROXY``, which aiohttp
                    # never did, and route this traffic somewhere unannounced.
                    trust_env=False,
                )
            return self._session

    async def aclose(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()

    async def request(
        self,
        method: str,
        url: str,
        *,
        expected_status: tuple[int, ...] = (200,),
        **kwargs: Any,
    ) -> Response:
        session = await self._ensure_session()
        via_proxy = bool(kwargs.get("proxy") or kwargs.get("proxies"))
        attempt = 0
        last_error: Optional[BaseException] = None
        while attempt <= self._retries:
            try:
                response = await session.request(
                    method, url, timeout=self._timeout_seconds, **kwargs
                )
                if response.status_code in expected_status:
                    return response
                if response.status_code in {429, 500, 502, 503, 504}:
                    last_error = RuntimeError(f"{method} {url} returned {response.status_code}")
                    if attempt >= self._retries:
                        break
                    await asyncio.sleep(min(2 ** attempt, 8))
                    attempt += 1
                    continue
                raise RuntimeError(
                    f"{method} {url} returned {response.status_code}: {response.text[:200]}"
                )
            except (CurlError, asyncio.TimeoutError) as exc:
                last_error = _proxy_aware_error(exc, via_proxy=via_proxy)
                if attempt >= self._retries:
                    break
                await asyncio.sleep(min(2 ** attempt, 8))
                attempt += 1
        # Chained, so callers can still see the original curl error -- a refused
        # proxy and an unreachable host read identically as text otherwise.
        raise RuntimeError(f"HTTP request failed for {url}: {last_error}") from last_error

    async def get_text(self, url: str, **kwargs: Any) -> tuple[str, Dict[str, str], int]:
        response = await self.request("GET", url, expected_status=(200, 304), **kwargs)
        return response.text, dict(response.headers), response.status_code

    async def get_json(self, url: str, **kwargs: Any) -> tuple[Any, Dict[str, str], int]:
        response = await self.request("GET", url, expected_status=(200, 304), **kwargs)
        return response.json(), dict(response.headers), response.status_code

    async def post_json(self, url: str, **kwargs: Any) -> tuple[Any, Dict[str, str], int]:
        response = await self.request("POST", url, expected_status=(200, 201, 304), **kwargs)
        if response.status_code == 304:
            return None, dict(response.headers), response.status_code
        return response.json(), dict(response.headers), response.status_code
