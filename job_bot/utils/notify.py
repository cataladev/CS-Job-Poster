from __future__ import annotations

import logging
import os
from typing import Mapping, Optional, Protocol

import aiohttp

# Operational alerts are worth waiting a moment for, but never worth stalling a poll.
WEBHOOK_TIMEOUT_SECONDS = 5
# Discord rejects a webhook body over 2000 characters outright.
WEBHOOK_CONTENT_LIMIT = 1900


class Notifier(Protocol):
    """Somewhere an operational message can be sent."""

    async def send(self, message: str) -> bool:
        ...


class LoggingNotifier:
    """Fallback used when no webhook is configured: the message still reaches the logs."""

    def __init__(self, logger_name: str = "notify") -> None:
        self._logger = logging.getLogger(logger_name)

    async def send(self, message: str) -> bool:
        self._logger.info("notification %s", message)
        return True


class WebhookNotifier:
    """Post a plain-text message to a Discord-compatible webhook.

    A failing alert path must never take the poll down with it, so every error is
    swallowed after being logged. The URL is a credential -- it is never logged, and
    only the host is named when reporting a failure.
    """

    def __init__(self, url: str, *, timeout_seconds: int = WEBHOOK_TIMEOUT_SECONDS) -> None:
        self._url = url
        self._timeout_seconds = timeout_seconds
        self._logger = logging.getLogger(self.__class__.__name__)

    async def send(self, message: str) -> bool:
        payload = {
            "content": message[:WEBHOOK_CONTENT_LIMIT],
            # Status lines quote upstream error text. An empty parse list forbids
            # every mention type, so no quoted string can ping @everyone or @here.
            "allowed_mentions": {"parse": []},
        }
        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self._url, json=payload) as response:
                    if response.status >= 400:
                        self._logger.warning(
                            "webhook_rejected status=%s host=%s",
                            response.status,
                            _webhook_host(self._url),
                        )
                        return False
            return True
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            self._logger.warning(
                "webhook_unreachable host=%s error=%s", _webhook_host(self._url), exc
            )
            return False


def _webhook_host(url: str) -> str:
    """The webhook's host, so a failure is diagnosable without leaking its token."""
    from urllib.parse import urlsplit

    return urlsplit(url).hostname or "unknown"


def load_notifier(
    url_env: str,
    *,
    logger_name: str = "notify",
    environ: Optional[Mapping[str, str]] = None,
) -> Notifier:
    """Build a webhook notifier from the environment, or a logging one if unset."""
    env = os.environ if environ is None else environ
    url = env.get(url_env, "").strip()
    if not url:
        return LoggingNotifier(logger_name)
    return WebhookNotifier(url)
