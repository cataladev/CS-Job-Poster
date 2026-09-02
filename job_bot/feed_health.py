from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from job_bot.utils.notify import Notifier

# Truncated so one upstream error cannot crowd out the rest of an alert.
ERROR_DETAIL_LIMIT = 300


# Green for a working source, red for a refused one, orange for a move between
# addresses that has not yet resolved either way.
KIND_BADGES = {
    "ready": "🟢",
    "recovered": "🟢",
    "degraded": "🔴",
    "rotated": "🟠",
}


@dataclass(frozen=True)
class HealthEvent:
    """One transition worth telling an operator about."""

    kind: str  # "ready" | "degraded" | "rotated" | "recovered"
    source: str
    message: str
    egress: str
    reason: Optional[str] = None
    consecutive_failures: int = 0
    downtime_seconds: Optional[float] = None
    status: Optional[int] = None


def format_status(status: Optional[int]) -> str:
    """Render an HTTP status, or say plainly that the request never got one.

    Not every failure has a status. A timeout or a TLS error is refused before any
    response exists, and reporting a number there would be a guess.
    """
    return f"HTTP {status}" if status is not None else "no response"


class SourceHealthReporter:
    """Announce a source's transitions -- not its every poll.

    A healthy feed that keeps working, and a blocked feed that keeps failing for the
    same reason, both say nothing: an alert channel that repeats itself every 45
    minutes stops being read. What gets announced is the first success after startup,
    a move into failure, a change in *why* it is failing, each egress rotation, and
    the recovery.
    """

    def __init__(
        self,
        source: str,
        notifier: Notifier,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.source = source
        self._notifier = notifier
        self._clock = clock
        self._logger = logging.getLogger(self.__class__.__name__)
        # None means "nothing observed yet", which is what separates the boot
        # announcement from an ordinary recovery.
        self._healthy: Optional[bool] = None
        self._failing_since: Optional[float] = None
        self._last_reason: Optional[str] = None
        self._consecutive_failures = 0

    @property
    def healthy(self) -> Optional[bool]:
        return self._healthy

    async def record_success(
        self, *, egress: str, observations: int, status: Optional[int] = None
    ) -> Optional[HealthEvent]:
        """Record a good window, announcing a boot or a recovery."""
        previously = self._healthy
        downtime = (
            self._clock() - self._failing_since if self._failing_since is not None else None
        )
        failures = self._consecutive_failures
        self._healthy = True
        self._failing_since = None
        self._last_reason = None
        self._consecutive_failures = 0

        if previously is None:
            return await self._emit(
                HealthEvent(
                    kind="ready",
                    source=self.source,
                    message=(
                        f"{self.source} online — {format_status(status)}, "
                        f"{_count(observations, 'job')} in the first window, "
                        f"egress {egress}"
                    ),
                    egress=egress,
                    status=status,
                )
            )
        if previously is False:
            return await self._emit(
                HealthEvent(
                    kind="recovered",
                    source=self.source,
                    message=(
                        f"{self.source} recovered — {format_status(status)}, "
                        f"{_count(observations, 'job')}, egress {egress}, after "
                        f"{_count(failures, 'failed attempt')} over "
                        f"{_format_duration(downtime)}"
                    ),
                    egress=egress,
                    status=status,
                    consecutive_failures=failures,
                    downtime_seconds=downtime,
                )
            )
        return None

    async def record_failure(
        self,
        *,
        egress: str,
        reason: str,
        error: BaseException | str,
        consecutive_failures: int,
        next_attempt_seconds: Optional[int] = None,
        status: Optional[int] = None,
    ) -> Optional[HealthEvent]:
        """Record a refused window, announcing entry into failure or a changed cause."""
        first_failure = self._healthy is not False
        reason_changed = reason != self._last_reason
        if self._failing_since is None:
            self._failing_since = self._clock()
        self._healthy = False
        self._last_reason = reason
        self._consecutive_failures = consecutive_failures

        if not (first_failure or reason_changed):
            self._logger.info(
                "source_still_failing source=%s reason=%s consecutive_failures=%s",
                self.source,
                reason,
                consecutive_failures,
            )
            return None

        retry_line = (
            f", next attempt in {_format_duration(next_attempt_seconds)}"
            if next_attempt_seconds is not None
            else ""
        )
        return await self._emit(
            HealthEvent(
                kind="degraded",
                source=self.source,
                message=(
                    f"{self.source} degraded — {reason} ({format_status(status)}) "
                    f"via egress {egress} (attempt {consecutive_failures})"
                    f"{retry_line}\n"
                    f"error: {str(error)[:ERROR_DETAIL_LIMIT]}"
                ),
                egress=egress,
                status=status,
                reason=reason,
                consecutive_failures=consecutive_failures,
            )
        )

    async def record_rotation(
        self,
        *,
        from_egress: str,
        to_egress: str,
        reason: str,
        error: BaseException | str,
        status: Optional[int] = None,
    ) -> HealthEvent:
        """Announce a move to the next egress address within one attempt."""
        return await self._emit(
            HealthEvent(
                kind="rotated",
                source=self.source,
                message=(
                    f"{self.source} rotated egress — {reason} ({format_status(status)}) "
                    f"on {from_egress}, retrying via {to_egress}\n"
                    f"error: {str(error)[:ERROR_DETAIL_LIMIT]}"
                ),
                egress=to_egress,
                status=status,
                reason=reason,
            )
        )

    async def _emit(self, event: HealthEvent) -> HealthEvent:
        self._logger.info(
            "source_health source=%s kind=%s egress=%s reason=%s",
            event.source,
            event.kind,
            event.egress,
            event.reason,
        )
        # A broken alert path must not break polling; the notifier logs its own errors.
        badge = KIND_BADGES.get(event.kind, "")
        await self._notifier.send(f"{badge} {event.message}".strip())
        return event


def _count(value: int, noun: str) -> str:
    """Pluralize a count, so an alert never reads "1 jobs"."""
    return f"{value} {noun}" if value == 1 else f"{value} {noun}s"


def _format_duration(seconds: Optional[float]) -> str:
    """Human-readable duration for an alert line."""
    if seconds is None:
        return "unknown"
    total = int(max(0.0, seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    hours, minutes = divmod(total // 60, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"
