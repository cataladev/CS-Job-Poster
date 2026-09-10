import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from job_bot.github_feeds import GitHubFeedFetcher
from job_bot.log_threads import CoreLogNotifier, LogThreads
from job_bot.scrape_status import ScrapeStatusReporter
from test_feed_publisher_mvp import FakeChannel, FakeClient, FakeMessage


def setup_logs(config_factory):
    config = config_factory()
    config.discord_log_channel_id = 333
    config.bot_environment = "arthur_dev"
    channel = FakeChannel([], channel_id=333)
    router = LogThreads(config)
    router.bind_client(FakeClient({333: channel}))
    return router, channel


@pytest.mark.asyncio
async def test_startup_and_concurrent_scrapes_share_two_permanent_threads(config_factory):
    router, channel = setup_logs(config_factory)
    reporter = ScrapeStatusReporter(router.config, log_threads=router)
    reporter.bind_client(router.client)
    await asyncio.gather(
        router.ensure_threads(),
        reporter.report(feed_group="core", elapsed_seconds=1, stats=SimpleNamespace()),
        reporter.report(feed_group="company_sources", elapsed_seconds=1,
                        stats=SimpleNamespace(errors=["ibm: HTTP 500"])),
    )
    await reporter.report(feed_group="core", elapsed_seconds=1, stats=SimpleNamespace())
    assert [card.thread.name for card in channel.messages] == ["Core feeds", "Direct scrapers"]
    core, direct = [card.thread for card in channel.messages]
    assert len(core.sent) == 2 and len(direct.sent) == 1
    assert all("Core feeds" in embed.title for embed in core.sent)
    assert "IBM" in direct.sent[0].title
    assert direct.contents == ["@here"]
    assert direct.allowed_mentions[0].everyone is True
    assert all(mentions.everyone is False for mentions in core.allowed_mentions)
    assert len(channel.sent) == 2  # Parent contains only permanent starter cards.
    assert all(mentions.everyone is False for mentions in channel.allowed_mentions)


@pytest.mark.asyncio
async def test_restart_recovers_archived_threads_across_days_and_bot_environments(config_factory):
    router, channel = setup_logs(config_factory)
    await router.ensure_threads()
    original = [card.thread for card in channel.messages]
    for card in channel.messages:
        card.created_at = datetime(2020, 1, 1, tzinfo=UTC)
        card.author.bot = True
        card.thread.archived = True
    restarted = LogThreads(router.config)
    restarted.config.bot_environment = "prod"
    other_client = FakeClient({333: channel})
    other_client.user = SimpleNamespace(id=555)
    restarted.bind_client(other_client)
    await restarted.ensure_threads()
    assert await restarted.thread_for("core") is original[0]
    assert await restarted.thread_for("company_sources") is original[1]
    assert await restarted.thread_for("direct_ats") is original[1]
    assert len(channel.messages) == 2
    assert channel.history_kwargs == {"limit": None, "oldest_first": False}


@pytest.mark.asyncio
async def test_interrupted_thread_creation_reuses_existing_card(config_factory, monkeypatch):
    router, channel = setup_logs(config_factory)
    original = FakeMessage.create_thread
    monkeypatch.setattr(FakeMessage, "create_thread", AsyncMock(side_effect=asyncio.TimeoutError()))
    await router.ensure_threads()
    assert len(channel.messages) == 1
    monkeypatch.setattr(FakeMessage, "create_thread", original)
    await router.ensure_threads()
    assert len(channel.messages) == 2
    assert all(card.thread is not None for card in channel.messages)


@pytest.mark.asyncio
async def test_uncertain_card_send_is_recovered_without_duplicate(config_factory):
    router, channel = setup_logs(config_factory)
    original = channel.send

    async def uncertain(**kwargs):
        await original(**kwargs)
        raise asyncio.TimeoutError("lost response after Discord accepted starter card")

    channel.send = uncertain
    await router.ensure_threads()
    channel.send = original
    await router.ensure_threads()
    assert len(channel.messages) == 2


@pytest.mark.asyncio
async def test_uncertain_thread_creation_is_recovered_without_duplicate(config_factory, monkeypatch):
    router, channel = setup_logs(config_factory)
    original = FakeMessage.create_thread

    async def uncertain(self, **kwargs):
        await original(self, **kwargs)
        raise asyncio.TimeoutError("lost response after Discord accepted thread")

    monkeypatch.setattr(FakeMessage, "create_thread", uncertain)
    await router.ensure_threads()
    first_thread = channel.messages[0].thread
    monkeypatch.setattr(FakeMessage, "create_thread", original)
    await router.ensure_threads()
    assert len(channel.messages) == 2
    assert await router.thread_for("core") is first_thread


@pytest.mark.asyncio
async def test_user_authored_marker_does_not_redirect_bot_logs(config_factory):
    router, channel = setup_logs(config_factory)
    await router.ensure_threads()
    channel.messages[0].author = SimpleNamespace(id=999, bot=False)
    await router.invalidate()
    await router.ensure_threads()
    assert len(channel.messages) == 3
    assert await router.thread_for("core") is channel.messages[-1].thread


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", ["create_public_threads", "send_messages_in_threads", "read_message_history"])
async def test_missing_thread_permissions_leave_errors_in_application_logs(config_factory, permission, caplog):
    router, channel = setup_logs(config_factory)
    setattr(channel.permissions, permission, False)
    await router.ensure_threads()
    assert await CoreLogNotifier(router).send("Jobright online") is False
    assert not channel.sent
    assert permission in caplog.text


@pytest.mark.asyncio
async def test_locked_threads_never_fall_back_to_parent_channel(config_factory):
    router, channel = setup_logs(config_factory)
    reporter = ScrapeStatusReporter(router.config, log_threads=router)
    reporter.bind_client(router.client)
    await router.ensure_threads()
    for card in channel.messages:
        card.thread.locked = True
    assert await reporter.report(feed_group="company_sources", elapsed_seconds=1,
                                 stats=SimpleNamespace()) is None
    assert await CoreLogNotifier(router).send("Jobright online") is False
    assert len(channel.sent) == 2
    assert all(not card.thread.sent for card in channel.messages)


@pytest.mark.asyncio
async def test_health_and_session_alerts_use_core_thread_even_with_webhook_configured(config_factory, monkeypatch):
    router, channel = setup_logs(config_factory)
    await router.ensure_threads()
    core = channel.messages[0].thread
    core.send = AsyncMock()
    monkeypatch.setenv("JOBRIGHT_STATUS_WEBHOOK_URL", "https://discord.com/api/webhooks/unused")
    monkeypatch.setenv("JOBRIGHT_ALERT_USER_ID", "424242424242424242")
    notifier = CoreLogNotifier(router)
    fetcher = GitHubFeedFetcher(jobright_notifier=notifier, jobright_proxies=())
    await fetcher._jobright_health.record_success(egress="direct", observations=30, status=200)
    # Both the health reporter and the link resolver receive the runtime's notifier.
    assert fetcher._jobright_links._notifier is notifier
    await fetcher._jobright_links._notifier.send("Jobright session expired — links have fallen back")
    await fetcher._jobright_health.record_failure(
        egress="direct", reason="blocked", error="@everyone @here", consecutive_failures=1,
    )
    online, session, failure = [call.kwargs for call in core.send.call_args_list]
    assert online["content"].startswith("[ARTHUR_DEV] 🟢 Jobright online")
    assert "session expired" in session["content"]
    assert online["allowed_mentions"].users == []
    assert failure["allowed_mentions"].to_dict() == {
        "parse": [], "users": [424242424242424242],
    }
    assert failure["allowed_mentions"].replied_user is False
    assert not channel.messages[1].thread.sent
    assert len(channel.sent) == 2


@pytest.mark.asyncio
async def test_disabled_destination_keeps_notifications_in_application_logs(config_factory, caplog):
    router, channel = setup_logs(config_factory)
    router.config.discord_log_channel_id = None
    await router.ensure_threads()
    with caplog.at_level("INFO"):
        assert await CoreLogNotifier(router).send("Jobright online") is True
    assert "Jobright online" in caplog.text
    assert not channel.sent


@pytest.mark.asyncio
async def test_log_channel_change_creates_only_its_own_pair(config_factory):
    router, first = setup_logs(config_factory)
    await router.ensure_threads()
    second = FakeChannel([], channel_id=444)
    router.client.channels[444] = second
    router.config.discord_log_channel_id = 444
    await router.ensure_threads()
    assert await router.thread_for("core") is second.messages[0].thread
    router.config.discord_log_channel_id = 333
    assert await router.thread_for("core") is first.messages[0].thread
    assert len(first.messages) == len(second.messages) == 2
