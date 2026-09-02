from __future__ import annotations

import pytest

from job_bot.feed_health import SourceHealthReporter, _count, _format_duration
from job_bot.utils.notify import WebhookNotifier, load_notifier


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> bool:
        self.messages.append(message)
        return True


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _reporter() -> tuple[SourceHealthReporter, RecordingNotifier, FakeClock]:
    notifier = RecordingNotifier()
    clock = FakeClock()
    return SourceHealthReporter("Jobright", notifier, clock=clock), notifier, clock


@pytest.mark.asyncio
async def test_first_success_announces_the_source_is_online() -> None:
    reporter, notifier, _ = _reporter()

    await reporter.record_success(egress="isp-a.example.com:8080", observations=412)

    assert len(notifier.messages) == 1
    assert "Jobright online" in notifier.messages[0]
    assert "412 jobs" in notifier.messages[0]
    assert "isp-a.example.com:8080" in notifier.messages[0]


@pytest.mark.asyncio
async def test_a_healthy_source_stays_quiet_after_the_first_success() -> None:
    reporter, notifier, _ = _reporter()

    await reporter.record_success(egress="direct", observations=412)
    await reporter.record_success(egress="direct", observations=408)
    await reporter.record_success(egress="direct", observations=399)

    # An alert channel that repeats itself every 45 minutes stops being read.
    assert len(notifier.messages) == 1


@pytest.mark.asyncio
async def test_failure_names_the_reason_egress_error_and_next_attempt() -> None:
    reporter, notifier, _ = _reporter()
    await reporter.record_success(egress="isp-a.example.com:8080", observations=400)

    await reporter.record_failure(
        egress="isp-a.example.com:8080",
        reason="blocked",
        error=RuntimeError("POST https://jobright.ai/... returned 403"),
        consecutive_failures=1,
        next_attempt_seconds=45 * 60,
    )

    message = notifier.messages[-1]
    assert "Jobright degraded" in message
    assert "blocked" in message
    assert "isp-a.example.com:8080" in message
    assert "returned 403" in message
    assert "next attempt in 45m" in message


@pytest.mark.asyncio
async def test_repeated_identical_failures_are_announced_once() -> None:
    reporter, notifier, _ = _reporter()

    await reporter.record_failure(
        egress="direct", reason="blocked", error="403", consecutive_failures=1
    )
    await reporter.record_failure(
        egress="direct", reason="blocked", error="403", consecutive_failures=2
    )

    assert len(notifier.messages) == 1


@pytest.mark.asyncio
async def test_a_changed_failure_reason_is_announced_again() -> None:
    reporter, notifier, _ = _reporter()

    await reporter.record_failure(
        egress="direct", reason="blocked", error="403", consecutive_failures=1
    )
    await reporter.record_failure(
        egress="direct", reason="proxy_error", error="cannot connect", consecutive_failures=2
    )

    # A block turning into a dead proxy is a different problem with a different fix.
    assert len(notifier.messages) == 2
    assert "proxy_error" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_recovery_reports_the_failure_count_and_downtime() -> None:
    reporter, notifier, clock = _reporter()
    await reporter.record_success(egress="isp-a.example.com:8080", observations=400)
    await reporter.record_failure(
        egress="isp-a.example.com:8080",
        reason="blocked",
        error="403",
        consecutive_failures=1,
    )
    clock.advance(2 * 3600 + 15 * 60)
    await reporter.record_failure(
        egress="isp-a.example.com:8080",
        reason="blocked",
        error="403",
        consecutive_failures=2,
    )

    await reporter.record_success(egress="isp-b.example.com:8080", observations=395)

    message = notifier.messages[-1]
    assert "Jobright recovered" in message
    assert "395 jobs" in message
    assert "isp-b.example.com:8080" in message
    assert "2 failed attempts" in message
    assert "2h 15m" in message
    assert reporter.healthy is True


@pytest.mark.asyncio
async def test_rotation_names_both_addresses() -> None:
    reporter, notifier, _ = _reporter()

    await reporter.record_rotation(
        from_egress="isp-a.example.com:8080",
        to_egress="isp-b.example.com:8080",
        reason="blocked",
        error=RuntimeError("returned 403"),
    )

    message = notifier.messages[-1]
    assert "rotated egress" in message
    assert "blocked (no response) on isp-a.example.com:8080" in message
    assert message.startswith("🟠")
    assert "retrying via isp-b.example.com:8080" in message


def test_counts_are_pluralized_so_an_alert_never_reads_one_jobs() -> None:
    assert _count(1, "job") == "1 job"
    assert _count(0, "job") == "0 jobs"
    assert _count(2, "failed attempt") == "2 failed attempts"


def test_durations_read_as_operators_write_them() -> None:
    assert _format_duration(45) == "45s"
    assert _format_duration(45 * 60) == "45m"
    assert _format_duration(3 * 3600) == "3h"
    assert _format_duration(2 * 3600 + 15 * 60) == "2h 15m"
    assert _format_duration(None) == "unknown"


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *_args) -> None:
        return None


class _FakeSession:
    posted: list[tuple[str, dict]] = []

    def __init__(self, *, status: int = 204, error: Exception | None = None, **_kwargs) -> None:
        self._status = status
        self._error = error

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    def post(self, url: str, *, json: dict) -> _FakeResponse:
        _FakeSession.posted.append((url, json))
        if self._error is not None:
            raise self._error
        return _FakeResponse(self._status)


@pytest.mark.asyncio
async def test_webhook_notifier_posts_the_message_and_caps_its_length(monkeypatch) -> None:
    _FakeSession.posted = []
    monkeypatch.setattr("job_bot.utils.notify.aiohttp.ClientSession", _FakeSession)

    sent = await WebhookNotifier("https://discord.com/api/webhooks/secret").send("x" * 5000)

    assert sent is True
    url, payload = _FakeSession.posted[-1]
    assert url == "https://discord.com/api/webhooks/secret"
    # Discord rejects a body over 2000 characters outright.
    assert len(payload["content"]) == 1900


@pytest.mark.asyncio
async def test_webhook_can_never_ping_everyone_or_here(monkeypatch) -> None:
    _FakeSession.posted = []
    monkeypatch.setattr("job_bot.utils.notify.aiohttp.ClientSession", _FakeSession)

    # Status lines quote upstream error text, which this bot does not author.
    sent = await WebhookNotifier("https://discord.com/api/webhooks/secret").send(
        "Jobright degraded: @everyone @here upstream said so"
    )

    assert sent is True
    _url, payload = _FakeSession.posted[-1]
    # An empty parse list forbids every mention type, whatever the content says.
    assert payload["allowed_mentions"] == {"parse": []}


@pytest.mark.asyncio
async def test_webhook_failures_never_reach_the_caller(monkeypatch, caplog) -> None:
    import aiohttp

    def failing_session(**kwargs):
        return _FakeSession(error=aiohttp.ClientError("connection reset"), **kwargs)

    monkeypatch.setattr("job_bot.utils.notify.aiohttp.ClientSession", failing_session)

    with caplog.at_level("WARNING"):
        sent = await WebhookNotifier("https://discord.com/api/webhooks/secret").send("hello")

    assert sent is False
    # The URL is a credential: a failure names the host and nothing more.
    assert "discord.com" in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_an_unset_webhook_falls_back_to_logging(caplog) -> None:
    notifier = load_notifier("JOBRIGHT_STATUS_WEBHOOK_URL", environ={})

    with caplog.at_level("INFO"):
        assert await notifier.send("Jobright online") is True

    assert "Jobright online" in caplog.text
