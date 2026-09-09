from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import socket
from datetime import UTC, datetime
from typing import Any

import discord

from job_bot.models import AppConfig
from job_bot.scrape_diagnostics import ScrapeFailure, ScrapeSourceResult


GROUP_LABELS = {
    "core": "Core feeds",
    "company_sources": "Company sources",
}


def _truncate(value: object, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _source_label(failure: ScrapeFailure, group_label: str) -> str:
    if failure.source:
        source = failure.source.split(" (")[0].removeprefix("company_api_")
        words = source.replace("_", " ").replace("-", " ").split()
        return " ".join(
            word.upper() if word.lower() in {"ibm", "api", "ats", "us"} else word.title()
            for word in words
        )
    if "database" in failure.stage.lower() or "database.py" in failure.location:
        return "Database"
    if "publish" in failure.stage.lower():
        return "Publishing"
    return f"{group_label} pipeline"


def _reason(failure: ScrapeFailure) -> str:
    message = " ".join(failure.message.split())
    if "timeout" in failure.error_type.lower() or "timed out" in message.lower():
        duration = re.search(
            r"(?:after|within)\s+(\d+(?:\.\d+)?)\s*(?:seconds?|s)\b", message, re.I
        )
        return f"No response within {duration[1]} seconds." if duration else "The request timed out."
    if "connection is closed" in message.lower() and (
        "database" in failure.stage.lower()
        or "database.py" in failure.location
        or failure.error_type in {"OperationalError", "InterfaceError"}
    ):
        return "The database connection is closed."
    status = re.search(r"\bHTTP\s+(\d{3})\b", message, re.I)
    if status:
        explanation = {
            "403": "Access denied", "404": "Endpoint not found", "429": "Rate limit reached",
            "500": "Server error", "502": "Bad gateway", "503": "Service unavailable",
            "504": "Gateway timed out",
        }.get(status[1])
        if explanation:
            return f"{explanation} (HTTP {status[1]})."
    return message if message and message != "No error message supplied" else "No error message was provided."


def _code(value: str, limit: int) -> str:
    # Keep upstream messages from breaking out of the inline code formatting.
    text = " ".join(value.split()).replace("`", "'")
    return f"`{_truncate(text, limit)}`"


def _next_run_in(
    next_due_at: datetime | None,
    *,
    now: datetime,
    elapsed_seconds: float,
    poll_interval_seconds: int,
) -> str:
    if next_due_at is None:
        return "unknown"
    interval = max(1, poll_interval_seconds)
    next_poll = max(1.0, interval - elapsed_seconds)
    # A feed becoming due between ticks waits until the next polling tick.
    extra_ticks = max(0, math.ceil(((next_due_at - now).total_seconds() - next_poll) / interval))
    seconds = math.ceil(next_poll + extra_ticks * interval)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = [f"{hours}h"] if hours else []
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def _add_source_counts_to_summary(embed: discord.Embed, sources: list[ScrapeSourceResult]) -> None:
    counts: dict[str, tuple[int, int]] = {}
    for source in sources:
        if source.new_jobs > 0 or source.duplicates > 0:
            provider = source.provider_label or source.label
            new, dupes = counts.get(provider, (0, 0))
            counts[provider] = (new + source.new_jobs, dupes + source.duplicates)
    if not counts:
        return

    summary = embed.fields[-1]
    # Leave room for the source roster and its omission notice on large failures.
    budget = min(1024 - len(summary.value) - 2, 6000 - len(embed) - 302)
    if budget < 100:
        return
    providers = sorted(counts.items(), key=lambda item: (-item[1][0], -item[1][1], item[0].casefold()))
    lines = []
    for provider, (new, dupes) in providers:
        label = _truncate(discord.utils.escape_markdown(" ".join(provider.split())), 100)
        line = f"**{label}:** **{new} new** · **{dupes} {'dupe' if dupes == 1 else 'dupes'}**"
        if len("\n".join([*lines, line])) > budget - 100:
            break
        lines.append(line)
    omitted = providers[len(lines):]
    if omitted:
        new = sum(count[0] for _, count in omitted)
        dupes = sum(count[1] for _, count in omitted)
        lines.append(f"+ {new} new · {dupes} dupes from {len(omitted)} more sources; see application logs.")
    embed.set_field_at(
        index=len(embed.fields) - 1, name=summary.name,
        value=summary.value + "\n\n" + "\n".join(lines), inline=False,
    )


def _add_source_fields(
    embed: discord.Embed,
    sources: list[ScrapeSourceResult],
    failures: list[ScrapeFailure],
    *,
    now: datetime,
    elapsed_seconds: float,
    poll_interval_seconds: int,
    company_sources: bool,
) -> None:
    """Give each source its own full-width field, keeping the run summary last."""
    failed_ids = {
        token
        for failure in failures if failure.source
        for token in re.split(r"[ ()]+", failure.source) if token
    }
    rows = []
    # Match the emoji plus its following space in Discord's proportional font.
    # Non-breaking spaces keep that hanging indent visible when rendered.
    detail_indent = "\u00a0" * 7
    order = {"failed": 0, "fetched": 1, "unchanged": 2, "skipped": 3, "not_run": 4}
    for source in sources:
        # A successful fetch can still fail later during ingestion or recording.
        status = "failed" if source.source_id in failed_ids else source.status
        icon, detail = {
            "failed": ("❌", "failed: see error details"),
            "fetched": ("✅", f"fetched · {source.observations} jobs read"),
            "unchanged": ("↔️", "checked, unchanged"),
            "skipped": ("⏭️", f"skipped: {_truncate(source.skip_reason, 40)}"),
            "not_run": ("⏹️", "not run"),
        }[status]
        label = _truncate(discord.utils.escape_markdown(" ".join(source.label.split())), 100)
        countdown = _next_run_in(
            now if company_sources else source.next_due_at,
            now=now, elapsed_seconds=elapsed_seconds, poll_interval_seconds=poll_interval_seconds,
        )
        # Field names are already bold. The zero-width prefix prevents trimming
        # the hanging indent at the beginning of the field value.
        rows.append((
            order[status], f"{icon} {label}:",
            f"\u200b{detail_indent}— {detail} · next run in: **{countdown}**",
        ))
    rows.sort(key=lambda item: item[0])
    for index, (_, name, value) in enumerate(rows):
        notice_name = "Additional sources"
        notice_value = f"+ {len(rows) - index} more sources; see application logs."
        # Reserve a field and enough text for an omission notice while more
        # sources remain. The existing summary is included in both budgets.
        reserve_fields = int(index < len(rows) - 1)
        reserve_chars = (len(notice_name) + len(notice_value)) if reserve_fields else 0
        if (len(embed.fields) + 1 + reserve_fields > 25
                or len(embed) + len(name) + len(value) + reserve_chars > 6000):
            embed.insert_field_at(
                index=len(embed.fields) - 1, name=notice_name, value=notice_value, inline=False,
            )
            break
        embed.insert_field_at(index=len(embed.fields) - 1, name=name, value=value, inline=False)


class ScrapeStatusReporter:
    """Send one quiet success or pinging failure update per scrape cycle."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.client: discord.Client | None = None
        self.logger = logging.getLogger(self.__class__.__name__)
        self.instance = f"{socket.gethostname()}:{os.getpid()}"

    def bind_client(self, client: discord.Client) -> None:
        self.client = client

    async def report(
        self,
        *,
        feed_group: str,
        elapsed_seconds: float,
        stats: Any | None,
        unexpected_error: Exception | None = None,
        poll_interval_seconds: int | None = None,
    ) -> int | None:
        channel_id = self.config.discord_log_channel_id
        if channel_id is None or self.client is None:
            return None

        failures = self._failures(stats, unexpected_error)
        failed = bool(failures)
        alert = failed and feed_group == "company_sources"
        embed = self._build_embed(
            feed_group=feed_group,
            elapsed_seconds=elapsed_seconds,
            stats=stats,
            failures=failures,
            failed=alert or unexpected_error is not None,
            poll_interval_seconds=poll_interval_seconds,
        )
        content = "@here" if alert else None
        allowed_mentions = (
            discord.AllowedMentions(
                everyone=True,
                users=False,
                roles=False,
                replied_user=False,
            )
            if alert
            else discord.AllowedMentions.none()
        )

        try:
            channel = self.client.get_channel(channel_id)
            if channel is None:
                channel = await self.client.fetch_channel(channel_id)
            if channel is None or not hasattr(channel, "send"):
                raise RuntimeError(f"Log destination {channel_id} is not a message channel")
            message = await channel.send(
                content=content,
                embed=embed,
                allowed_mentions=allowed_mentions,
            )
        except (RuntimeError, discord.HTTPException, asyncio.TimeoutError) as exc:
            self.logger.error(
                "scrape_status_send_failed environment=%s feed_group=%s channel=%s error=%s",
                self.config.bot_environment,
                feed_group,
                channel_id,
                exc,
            )
            return None

        self.logger.info(
            "scrape_status_sent environment=%s feed_group=%s failed=%s alert=%s errors=%s channel=%s message=%s",
            self.config.bot_environment,
            feed_group,
            failed,
            alert,
            len(failures),
            channel_id,
            message.id,
        )
        return int(message.id)

    @staticmethod
    def _failures(stats: Any | None, unexpected_error: Exception | None) -> list[ScrapeFailure]:
        failures = list(getattr(stats, "failure_details", []))
        if unexpected_error is not None:
            failures.insert(0, ScrapeFailure.from_exception(unexpected_error))
        if stats is not None:
            for error in [*getattr(stats, "errors", []), *getattr(stats, "source_errors", [])]:
                # The string lists remain available to baseline logic and older
                # callers. Prefer the matching diagnostic with its type/stage.
                if not error:
                    continue
                source, separator, message = str(error).partition(":")
                has_source = separator and re.fullmatch(r"[a-z0-9_-]+", source)
                source = source if has_source else None
                message = message.strip() if has_source else str(error)
                if any(
                    failure.source and failure.source.split(" (")[0] == source
                    and (failure.message in message or not message)
                    for failure in failures
                ):
                    continue
                failures.append(ScrapeFailure(
                    source=source, stage="", error_type="", message=message,
                ))
        return list(dict.fromkeys(failures))

    @staticmethod
    def _errors(stats: Any | None, unexpected_error: Exception | None) -> list[str]:
        return [failure.describe() for failure in ScrapeStatusReporter._failures(stats, unexpected_error)]

    def _build_embed(
        self,
        *,
        feed_group: str,
        elapsed_seconds: float,
        stats: Any | None,
        failures: list[ScrapeFailure],
        failed: bool,
        poll_interval_seconds: int | None = None,
    ) -> discord.Embed:
        group_label = GROUP_LABELS.get(feed_group, feed_group.replace("_", " ").title())
        environment = _truncate(self.config.bot_environment.upper(), 48)
        shown = failures[:5]
        if failures:
            labels = [_source_label(failure, group_label) for failure in shown]
            subject = labels[0] if len(failures) == 1 else f"{len(failures)} failures"
            severity = "Error" if failed else "Warning"
            title = f"{'🚨' if failed else '⚠️'} [{environment}] {_truncate(subject, 170)} — {severity}"
            summaries = []
            for failure, label in zip(shown, labels):
                reason = _truncate(discord.utils.escape_markdown(_reason(failure)), 230)
                heading = (
                    f"**{_truncate(discord.utils.escape_markdown(label), 100)}**\n"
                    if len(failures) > 1 else ""
                )
                summary = f"{heading}**Why:** {reason}"
                if failure.source is None and failure.stage == "database baseline check":
                    summary += "\nStopped before any scrapers ran."
                summaries.append(summary)
            description = "\n\n".join(summaries)
            if len(failures) > len(shown):
                description += f"\n\n+ {len(failures) - len(shown)} more errors; see application logs."
        else:
            title = f"✅ [{environment}] {group_label} scrape successful"
            description = "The scrape cycle completed without errors."

        now = datetime.now(UTC)
        embed = discord.Embed(
            title=title,
            description=description,
            color=(
                discord.Color.red()
                if failed
                else discord.Color.orange() if failures else discord.Color.green()
            ),
            timestamp=now,
        )
        for failure in shown:
            details = []
            if failure.source:
                details.append(f"**Source:** {_code(failure.source, 80)}")
            if failure.stage:
                details.append(f"**Stage:** {_truncate(discord.utils.escape_markdown(failure.stage), 60)}")
            error = f"{failure.error_type}: {failure.message}" if failure.error_type else failure.message
            details.append(f"**Error:**\n{_code(error, 200)}")
            if failure.location:
                details.append(f"**Code:** {_code(failure.location, 120)}")
            embed.add_field(
                name="Technical details" if len(failures) == 1 else f"{_truncate(_source_label(failure, group_label), 100)} — details",
                value="\n".join(details), inline=False,
            )
        checked = getattr(stats, "feeds_checked", getattr(stats, "sources_checked", 0))
        sources = list(getattr(stats, "source_results", []))
        error_label = "error" if len(failures) == 1 else "errors"
        source_label = "source" if checked == 1 else "sources"
        source_summary = (
            f"{len(sources)} configured {'source' if len(sources) == 1 else 'sources'}"
            if sources else f"{checked} {source_label}"
        )
        embed.add_field(name="Run summary", value=(
            f"{elapsed_seconds:.2f}s · {source_summary} · {len(failures)} {error_label}\n"
            f"{getattr(stats, 'observations', 0)} observations · "
            f"{getattr(stats, 'canonical_created', 0)} new · "
            f"{getattr(stats, 'duplicates', 0)} {'dupe' if getattr(stats, 'duplicates', 0) == 1 else 'dupes'} · "
            f"{getattr(stats, 'deliveries_queued', 0)} queued · "
            f"{getattr(stats, 'deliveries_sent', 0)} posted"
        ), inline=False)
        embed.set_footer(text=f"{group_label} • {_truncate(self.instance, 160)}")
        _add_source_counts_to_summary(embed, sources)
        if poll_interval_seconds is None:
            poll_interval_seconds = (
                self.config.company_source_poll_interval_seconds if feed_group == "company_sources"
                else self.config.poll_interval_seconds
            )
        _add_source_fields(
            embed, sources, failures, now=now, elapsed_seconds=elapsed_seconds,
            poll_interval_seconds=poll_interval_seconds, company_sources=feed_group == "company_sources",
        )
        return embed
