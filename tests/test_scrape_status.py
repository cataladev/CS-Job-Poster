from types import SimpleNamespace

import discord
import pytest

from job_bot.scrape_status import ScrapeStatusReporter


class FakeLogChannel:
    def __init__(self) -> None:
        self.messages = []

    async def send(self, *, content, embed, allowed_mentions):
        self.messages.append((content, embed, allowed_mentions))
        return SimpleNamespace(id=987)


class FakeClient:
    def __init__(self, channel_id: int, channel: FakeLogChannel) -> None:
        self.channel_id = channel_id
        self.channel = channel

    def get_channel(self, channel_id: int):
        return self.channel if channel_id == self.channel_id else None

    async def fetch_channel(self, channel_id: int):
        return self.get_channel(channel_id)


@pytest.mark.asyncio
async def test_successful_scrape_sends_a_quiet_status_embed(config_factory) -> None:
    config = config_factory()
    config.discord_log_channel_id = 1492951491106046002
    channel = FakeLogChannel()
    reporter = ScrapeStatusReporter(config)
    reporter.bind_client(FakeClient(config.discord_log_channel_id, channel))  # type: ignore[arg-type]
    stats = SimpleNamespace(
        feeds_checked=12,
        observations=41,
        canonical_created=3,
        deliveries_queued=2,
        deliveries_sent=2,
        errors=[],
        source_errors=[],
    )

    message_id = await reporter.report(
        feed_group="core",
        elapsed_seconds=1.25,
        stats=stats,
    )

    assert message_id == 987
    content, embed, allowed_mentions = channel.messages[0]
    assert content is None
    assert embed.title == "✅ Core feeds scrape successful"
    assert embed.color == discord.Color.green()
    assert "without errors" in embed.description
    assert {field.name: field.value for field in embed.fields} == {
        "Duration": "1.25s",
        "Sources checked": "12",
        "Observations": "41",
        "New jobs": "3",
        "Queued": "2",
        "Posted": "2",
    }
    assert allowed_mentions.everyone is False
    assert allowed_mentions.users is False
    assert allowed_mentions.roles is False


@pytest.mark.asyncio
async def test_reported_source_error_includes_details_and_pings_here(config_factory) -> None:
    config = config_factory()
    config.discord_log_channel_id = 1492951491106046002
    channel = FakeLogChannel()
    reporter = ScrapeStatusReporter(config)
    reporter.bind_client(FakeClient(config.discord_log_channel_id, channel))  # type: ignore[arg-type]
    stats = SimpleNamespace(
        sources_checked=1,
        observations=0,
        canonical_created=0,
        deliveries_queued=0,
        deliveries_sent=0,
        errors=["ibm: careers endpoint returned HTTP 500"],
        source_errors=[],
    )

    await reporter.report(
        feed_group="company_sources",
        elapsed_seconds=4.5,
        stats=stats,
    )

    content, embed, allowed_mentions = channel.messages[0]
    assert content == "@here"
    assert embed.title == "🚨 Company sources scrape failed"
    assert embed.color == discord.Color.red()
    assert "ibm: careers endpoint returned HTTP 500" in embed.description
    assert allowed_mentions.everyone is True
    assert allowed_mentions.users is False
    assert allowed_mentions.roles is False


@pytest.mark.asyncio
async def test_unexpected_core_exception_is_logged_without_pinging(config_factory) -> None:
    config = config_factory()
    config.discord_log_channel_id = 1492951491106046002
    channel = FakeLogChannel()
    reporter = ScrapeStatusReporter(config)
    reporter.bind_client(FakeClient(config.discord_log_channel_id, channel))  # type: ignore[arg-type]

    await reporter.report(
        feed_group="core",
        elapsed_seconds=0.1,
        stats=None,
        unexpected_error=RuntimeError("database connection lost"),
    )

    content, embed, allowed_mentions = channel.messages[0]
    assert content is None
    assert "RuntimeError: database connection lost" in embed.description
    assert allowed_mentions.everyone is False


@pytest.mark.asyncio
async def test_jobright_only_failure_is_logged_without_pinging_here(config_factory) -> None:
    config = config_factory()
    config.discord_log_channel_id = 1492951491106046002
    channel = FakeLogChannel()
    reporter = ScrapeStatusReporter(config)
    reporter.bind_client(FakeClient(config.discord_log_channel_id, channel))  # type: ignore[arg-type]
    stats = SimpleNamespace(
        feeds_checked=12,
        observations=0,
        canonical_created=0,
        deliveries_queued=0,
        deliveries_sent=0,
        errors=["jobright_internships: HTTP 503"],
        source_errors=["jobright_internships: HTTP 503"],
    )

    await reporter.report(
        feed_group="core",
        elapsed_seconds=2.0,
        stats=stats,
    )

    content, embed, allowed_mentions = channel.messages[0]
    assert content is None
    assert embed.title == "⚠️ Core feeds scrape completed with a warning"
    assert embed.color == discord.Color.orange()
    assert "jobright_internships: HTTP 503" in embed.description
    assert allowed_mentions.everyone is False


@pytest.mark.asyncio
async def test_mixed_core_feed_failures_do_not_ping_here(config_factory) -> None:
    config = config_factory()
    config.discord_log_channel_id = 1492951491106046002
    channel = FakeLogChannel()
    reporter = ScrapeStatusReporter(config)
    reporter.bind_client(FakeClient(config.discord_log_channel_id, channel))  # type: ignore[arg-type]
    stats = SimpleNamespace(
        feeds_checked=12,
        observations=0,
        canonical_created=0,
        deliveries_queued=0,
        deliveries_sent=0,
        errors=["jobright_new_grad: HTTP 503", "simplify_internships: HTTP 500"],
        source_errors=[],
    )

    await reporter.report(
        feed_group="core",
        elapsed_seconds=2.0,
        stats=stats,
    )

    content, embed, allowed_mentions = channel.messages[0]
    assert content is None
    assert embed.title == "⚠️ Core feeds scrape completed with a warning"
    assert "simplify_internships: HTTP 500" in embed.description
    assert allowed_mentions.everyone is False
