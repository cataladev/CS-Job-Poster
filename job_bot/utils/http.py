from __future__ import annotations

import asyncio
import base64
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence
from urllib.parse import unquote, urlsplit, urlunsplit

import aiohttp


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
        """Keyword arguments that route one aiohttp request through this proxy.

        The credentials go out as an explicit ``Proxy-Authorization`` header rather
        than through ``proxy_auth``, which aiohttp deprecated in 3.14 and removes in
        4.0. For an HTTPS target aiohttp carries these headers on the ``CONNECT``,
        which is where an authenticating proxy expects them.
        """
        kwargs: Dict[str, Any] = {"proxy": self.url}
        if self.username is not None:
            token = base64.b64encode(
                f"{self.username}:{self.password or ''}".encode("utf-8")
            ).decode("ascii")
            kwargs["proxy_headers"] = {"Proxy-Authorization": f"Basic {token}"}
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
