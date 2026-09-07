import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import discord
import pytest

from job_bot.scrape_status import ScrapeStatusReporter, _next_run_in
from job_bot.github_feeds import FEEDS
from job_bot.scrape_diagnostics import ScrapeFailure, ScrapeSourceResult


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
    assert embed.title == "✅ [UNKNOWN] Core feeds scrape successful"
    assert embed.color == discord.Color.green()
    assert "without errors" in embed.description
    assert {field.name: field.value for field in embed.fields} == {
        "Run summary": "1.25s · 12 sources · 0 errors\n41 observations · 3 new · 0 dupes · 2 queued · 2 posted",
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
    assert embed.title == "🚨 [UNKNOWN] IBM — Error"
    assert embed.color == discord.Color.red()
    assert embed.description == "**Why:** Server error (HTTP 500)."
    assert "careers endpoint returned HTTP 500" in embed.fields[0].value
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
    assert "RuntimeError: database connection lost" in embed.fields[0].value
    assert embed.title == "🚨 [UNKNOWN] Core feeds pipeline — Error"
    assert embed.description == "**Why:** database connection lost"
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
    assert embed.title == "⚠️ [UNKNOWN] Jobright Internships — Warning"
    assert embed.color == discord.Color.orange()
    assert embed.description == "**Why:** Service unavailable (HTTP 503)."
    assert "jobright_internships" in embed.fields[0].value
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
    assert embed.title == "⚠️ [UNKNOWN] 2 failures — Warning"
    assert "**Jobright New Grad**\n**Why:** Service unavailable (HTTP 503)." in embed.description
    assert "**Simplify Internships**\n**Why:** Server error (HTTP 500)." in embed.description
    assert allowed_mentions.everyone is False


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["dev", "prod", "arthur_dev"])
async def test_status_identifies_environment_source_stage_and_process(
    config_factory, caplog, environment
) -> None:
    config = config_factory()
    config.bot_environment = environment
    config.discord_log_channel_id = 123
    channel = FakeLogChannel()
    reporter = ScrapeStatusReporter(config)
    reporter.bind_client(FakeClient(123, channel))
    failure = ScrapeFailure(
        source="jobright_internships", stage="fetch feed", error_type="TimeoutError",
        message="request timed out", location="job_bot/github_feeds.py:100 in fetch",
    )
    stats = SimpleNamespace(
        errors=["jobright_internships: request timed out"],
        source_errors=["jobright_internships: request timed out"],
        failure_details=[failure],
    )
    with caplog.at_level(logging.INFO):
        await reporter.report(feed_group="core", elapsed_seconds=1, stats=stats)

    content, embed, _ = channel.messages[0]
    assert f"[{environment.upper()}]" in embed.title
    assert embed.title == f"⚠️ [{environment.upper()}] Jobright Internships — Warning"
    assert embed.description == "**Why:** The request timed out."
    assert "jobright_internships" in embed.fields[0].value
    assert "fetch feed" in embed.fields[0].value
    assert "TimeoutError: request timed out" in embed.fields[0].value
    assert failure.location in embed.fields[0].value
    assert reporter.instance in embed.footer.text
    assert "1 error\n" in embed.fields[-1].value
    assert content is None
    assert f"environment={environment}" in caplog.text
    assert "failed=True alert=False errors=1" in caplog.text


@pytest.mark.asyncio
async def test_large_errors_fit_discord_limits_and_report_omissions(config_factory) -> None:
    config = config_factory()
    config.discord_log_channel_id = 123
    channel = FakeLogChannel()
    reporter = ScrapeStatusReporter(config)
    reporter.bind_client(FakeClient(123, channel))
    stats = SimpleNamespace(errors=[f"source_{i}: " + "x" * 5000 for i in range(12)])

    await reporter.report(feed_group="company_sources", elapsed_seconds=1, stats=stats)

    _, embed, _ = channel.messages[0]
    assert len(embed) <= 6000
    assert len(embed.description) <= 4096
    assert "Source 0" in embed.description and "Source 4" in embed.description
    assert "+ 7 more errors" in embed.description
    assert "12 errors" in embed.fields[-1].value


@pytest.mark.asyncio
async def test_baseline_failure_names_database_and_explains_impact(config_factory):
    config = config_factory()
    config.bot_environment = "dev"
    config.discord_log_channel_id = 123
    channel = FakeLogChannel()
    reporter = ScrapeStatusReporter(config)
    reporter.bind_client(FakeClient(123, channel))
    error = RuntimeError("the connection is closed")
    error.scrape_failure = ScrapeFailure(
        source=None, stage="database baseline check", error_type="OperationalError",
        message=str(error), location="job_bot/feed_pipeline.py:153 in run",
    )
    await reporter.report(feed_group="core", elapsed_seconds=0.01, stats=None, unexpected_error=error)

    _, embed, _ = channel.messages[0]
    assert embed.title == "🚨 [DEV] Database — Error"
    assert embed.description == "**Why:** The database connection is closed.\nStopped before any scrapers ran."
    assert "OperationalError: the connection is closed" in embed.fields[0].value
    assert "0 sources" in embed.fields[-1].value


def test_same_reason_on_two_sources_keeps_both_scraper_names(config_factory):
    reporter = ScrapeStatusReporter(config_factory())
    stats = SimpleNamespace(
        failure_details=[ScrapeFailure(source="jobright_internships", stage="fetch feed", error_type="RuntimeError", message="HTTP 503")],
        errors=["jobright_internships: HTTP 503", "simplify_internships: HTTP 503"],
    )
    failures = reporter._failures(stats, None)
    assert len(failures) == 2
    embed = reporter._build_embed(feed_group="core", elapsed_seconds=1, stats=stats, failures=failures, failed=False)
    assert "Jobright Internships" in embed.description
    assert "Simplify Internships" in embed.description


def test_long_structured_failures_fit_discord_limits(config_factory):
    config = config_factory()
    config.bot_environment = "a_very_long_custom_environment_name_" * 20
    reporter = ScrapeStatusReporter(config)
    failures = [ScrapeFailure(
        source=f"company_{i}_" + "*" * 1000, stage="*" * 1000, error_type="ValueError",
        message="*" * 5000, location="long/path/" * 1000,
    ) for i in range(12)]
    embed = reporter._build_embed(feed_group="company_sources", elapsed_seconds=1, stats=None, failures=failures, failed=True)
    assert len(embed) <= 6000
    assert len(embed.title) <= 256
    assert len(embed.description) <= 4096
    assert all(len(field.name) <= 256 and len(field.value) <= 1024 for field in embed.fields)


def test_each_core_feed_has_its_own_field_and_run_summary_stays_separate(config_factory):
    sources = [ScrapeSourceResult(
        source_id=feed.source_id, label=feed.scrape_label,
        status="fetched" if feed.source_id.startswith("jobright") else "skipped",
        observations=15 if feed.source_id.startswith("jobright") else 0,
    ) for feed in FEEDS]
    stats = SimpleNamespace(feeds_checked=12, source_results=sources, observations=30)
    embed = ScrapeStatusReporter(config_factory())._build_embed(
        feed_group="core", elapsed_seconds=3, stats=stats, failures=[], failed=False,
    )
    source_fields = embed.fields[:-1]
    roster = "\n".join(f"{field.name}\n{field.value}" for field in source_fields)
    assert len(sources) == 12
    assert len(source_fields) == 12
    assert len({source.label for source in sources}) == 12
    for source in sources:
        icon = "✅" if source.status == "fetched" else "⏭️"
        field = next(field for field in source_fields if field.name == f"{icon} {source.label}:")
        assert field.value.startswith("\u200b" + "\u00a0" * 7 + "— ")
        assert "\n" not in field.value
    assert "Simplify · Off-season internships" in roster
    assert "Internship Engine · JSON feed" in roster
    assert "Internship Engine · README list" in roster
    assert roster.count("fetched · 15 jobs read") == 2
    assert roster.count("skipped: not due") == 10
    assert "12 configured sources" in embed.fields[-1].value
    assert "IBM" not in roster
    assert all(field.name != "New jobs found" for field in embed.fields)
    assert all(field.name not in {"Sources this cycle", "Sources (continued)", "\u200b"} for field in embed.fields)
    assert embed.fields[-1].name == "Run summary"
    assert sum(field.name == "Run summary" for field in embed.fields) == 1
    assert len(embed) <= 6000
    assert all(len(field.value) <= 1024 and not field.inline for field in embed.fields)


def test_source_rows_distinguish_unchanged_failed_and_not_run(config_factory):
    sources = [
        ScrapeSourceResult("same", "Unchanged feed", "unchanged"),
        ScrapeSourceResult("pending", "Pending feed"),
        ScrapeSourceResult("broken", "Broken feed", "fetched", observations=5),
        ScrapeSourceResult("company_api_ibm", "IBM", "fetched", observations=45),
    ]
    failures = [ScrapeFailure("broken", "ingest observations", "ValueError", "invalid row")]
    embed = ScrapeStatusReporter(config_factory())._build_embed(
        feed_group="core", elapsed_seconds=3,
        stats=SimpleNamespace(source_results=sources), failures=failures, failed=False,
    )
    assert "**Error:**\n`ValueError: invalid row`" in embed.fields[0].value
    source_fields = embed.fields[1:-1]
    assert [field.name for field in source_fields] == [
        "❌ Broken feed:", "✅ IBM:", "↔️ Unchanged feed:", "⏹️ Pending feed:",
    ]
    assert "— failed: see error details" in source_fields[0].value
    assert "— fetched · 45 jobs read" in source_fields[1].value
    assert "— checked, unchanged" in source_fields[2].value
    assert "— not run" in source_fields[3].value


def test_company_failure_matches_qualified_source_id(config_factory):
    embed = ScrapeStatusReporter(config_factory())._build_embed(
        feed_group="company_sources", elapsed_seconds=3,
        stats=SimpleNamespace(source_results=[ScrapeSourceResult("company_api_ibm", "IBM", "fetched")]),
        failures=[ScrapeFailure("ibm (company_api_ibm)", "record source success", "RuntimeError", "database closed")],
        failed=True,
    )
    assert embed.fields[-2].name == "❌ IBM:"
    assert "— failed: see error details" in embed.fields[-2].value


@pytest.mark.parametrize("with_failures", [False, True])
def test_long_roster_and_errors_fit_discord_limits_and_report_omissions(config_factory, with_failures):
    sources = [ScrapeSourceResult(
        f"source_{i}", "long * source name " * 100 if with_failures else f"Source {i}", "fetched", new_jobs=1,
        provider_label=f"provider_{i}_" + "*" * 1000, duplicates=123,
    ) for i in range(200)]
    failures = [ScrapeFailure(
        source=f"source_{i}" + "*" * 1000, stage="*" * 1000,
        error_type="ValueError", message="*" * 5000, location="long/path/" * 1000,
    ) for i in range(12)] if with_failures else []
    embed = ScrapeStatusReporter(config_factory())._build_embed(
        feed_group="core", elapsed_seconds=1,
        stats=SimpleNamespace(source_results=sources), failures=failures, failed=False,
    )
    assert len(embed) <= 6000
    assert len(embed.fields) <= 25
    assert all(len(field.name) <= 256 and len(field.value) <= 1024 for field in embed.fields)
    notice = embed.fields[-2]
    assert notice.name == "Additional sources"
    shown_sources = sum(field.name.startswith("✅ ") for field in embed.fields)
    assert notice.value == f"+ {200 - shown_sources} more sources; see application logs."
    if not with_failures:
        assert len(embed.fields) == 25
    assert "more sources; see application logs" in "\n".join(field.value for field in embed.fields)
    assert "200 configured sources" in embed.fields[-1].value
    summary = embed.fields[-1]
    assert summary.name == "Run summary"
    assert "**1 new** · **123 dupes**" in summary.value
    assert "more sources; see application logs" in summary.value


@pytest.mark.asyncio
async def test_new_jobs_and_dupes_are_grouped_by_provider_only_in_run_summary(config_factory):
    config = config_factory()
    config.discord_log_channel_id = 123
    channel = FakeLogChannel()
    reporter = ScrapeStatusReporter(config)
    reporter.bind_client(FakeClient(123, channel))
    stats = SimpleNamespace(
        observations=1120, canonical_created=13, duplicates=1106,
        source_results=[
            ScrapeSourceResult("jobright_internships", "Jobright · Internships", "fetched", 15,
                               new_jobs=10, provider_label="Jobright", duplicates=4),
            ScrapeSourceResult("jobright_new_grad", "Jobright · New grads", "fetched", 5,
                               new_jobs=2, provider_label="Jobright", duplicates=3),
            ScrapeSourceResult("simplify_internships", "Simplify · Internships", "fetched", 1100,
                               new_jobs=1, provider_label="Simplify", duplicates=1099),
        ],
    )
    await reporter.report(feed_group="core", elapsed_seconds=3, stats=stats)
    content, embed, mentions = channel.messages[0]
    assert content is None and not mentions.everyone
    assert all(field.name != "New jobs found" for field in embed.fields)
    summary = embed.fields[-1]
    assert summary.name == "Run summary"
    assert summary.value.endswith("**Jobright:** **12 new** · **7 dupes**\n**Simplify:** **1 new** · **1099 dupes**")
    assert "13 new · 1106 dupes" in summary.value
    roster = "\n".join(field.value for field in embed.fields[:-1])
    for observations in (15, 5, 1100):
        assert f"fetched · {observations} jobs read · next run in:" in roster
    assert " new**" not in roster and "dupes" not in roster


def test_new_jobs_and_dupes_remain_visible_when_a_company_source_partially_fails(config_factory):
    failure = ScrapeFailure("ibm (company_api_ibm)", "record source success", "RuntimeError", "database write failed")
    stats = SimpleNamespace(canonical_created=2, duplicates=40, source_results=[
        ScrapeSourceResult("company_api_ibm", "IBM", "failed", 45, new_jobs=2, duplicates=40),
    ])
    embed = ScrapeStatusReporter(config_factory())._build_embed(
        feed_group="company_sources", elapsed_seconds=3, stats=stats, failures=[failure], failed=True,
    )
    assert embed.fields[0].name == "Technical details"
    assert "**Error:**\n`RuntimeError: database write failed`" in embed.fields[0].value
    assert "failed: see error details · next run in:" in embed.fields[1].value
    assert embed.fields[-1].name == "Run summary"
    assert "**IBM:** **2 new** · **40 dupes**" in embed.fields[-1].value


@pytest.mark.parametrize(("duplicates", "label"), [(1, "dupe"), (12, "dupes")])
def test_dupe_only_runs_show_counts_without_counting_skipped_or_unchanged_sources(config_factory, duplicates, label):
    stats = SimpleNamespace(duplicates=duplicates, source_results=[
        ScrapeSourceResult("jobright_internships", "Jobright · Internships", "fetched",
                           observations=duplicates, provider_label="Jobright", duplicates=duplicates),
        ScrapeSourceResult("simplify_internships", "Simplify · Internships", "skipped"),
        ScrapeSourceResult("speedy_internships", "SpeedyApply · Internships", "unchanged"),
    ])
    embed = ScrapeStatusReporter(config_factory())._build_embed(
        feed_group="core", elapsed_seconds=3, stats=stats, failures=[], failed=False,
    )
    summary = embed.fields[-1]
    assert summary.name == "Run summary"
    assert summary.value.endswith(f"**Jobright:** **0 new** · **{duplicates} {label}**")
    assert "Simplify" not in summary.value and "SpeedyApply" not in summary.value


@pytest.mark.parametrize(("due_in", "elapsed", "interval", "expected"), [
    (147, 3, 60, "2m 57s"),  # Due between ticks: wait for the next actual poll.
    (30, 3, 60, "57s"),
    (57, 3, 60, "57s"),
    (58, 3, 60, "1m 57s"),
    (-10, 3, 60, "57s"),  # Due already, but the loop is still sleeping.
    (30, 90, 60, "1m 1s"),  # A run longer than the poll interval.
    (0, 4.4, 3600, "59m 56s"),
    (None, 3, 60, "unknown"),
])
def test_next_run_countdown_respects_poll_ticks(due_in, elapsed, interval, expected):
    now = datetime(2026, 9, 7, 12, 22, 3, tzinfo=UTC)
    due_at = now + timedelta(seconds=due_in) if due_in is not None else None
    assert _next_run_in(
        due_at, now=now, elapsed_seconds=elapsed, poll_interval_seconds=interval,
    ) == expected


def test_source_rows_show_remaining_wait_at_log_time(config_factory):
    config = config_factory()
    config.poll_interval_seconds = 60
    now = datetime(2026, 9, 7, 12, 22, 3, tzinfo=UTC)
    stats = SimpleNamespace(source_results=[
        ScrapeSourceResult("simplify_internships", "Simplify · Internships", "skipped",
                           next_due_at=now + timedelta(seconds=147)),
        ScrapeSourceResult("jobright_internships", "Jobright · Internships", "fetched", 15,
                           next_due_at=now + timedelta(seconds=30)),
    ])
    with patch("job_bot.scrape_status.datetime") as clock:
        clock.now.return_value = now
        embed = ScrapeStatusReporter(config)._build_embed(
            feed_group="core", elapsed_seconds=3, stats=stats, failures=[], failed=False,
        )
    fields = {field.name: field.value for field in embed.fields}
    assert fields["⏭️ Simplify · Internships:"] == "\u200b" + "\u00a0" * 7 + "— skipped: not due · next run in: **2m 57s**"
    assert fields["✅ Jobright · Internships:"] == "\u200b" + "\u00a0" * 7 + "— fetched · 15 jobs read · next run in: **57s**"


def test_company_countdown_uses_the_running_pollers_interval(config_factory):
    # The interval passed by the runtime takes priority over configuration defaults.
    stats = SimpleNamespace(source_results=[ScrapeSourceResult("company_api_ibm", "IBM", "fetched", 45)])
    embed = ScrapeStatusReporter(config_factory())._build_embed(
        feed_group="company_sources", elapsed_seconds=5, stats=stats,
        failures=[], failed=False, poll_interval_seconds=1800,
    )
    assert "next run in: **29m 55s**" in embed.fields[-2].value
