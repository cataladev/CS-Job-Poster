from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

import aiohttp


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
        raise RuntimeError(f"HTTP request failed for {url}: {last_error}")

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
