from __future__ import annotations

import asyncio
import csv
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from job_bot.branding import BrandingResolver
from job_bot.database import Database
from job_bot.models import AppConfig, CanonicalJob
from job_bot.utils.feed_normalization import is_us_location, sanitize_application_url
from job_bot.utils.http import AsyncHttpClient
from job_bot.utils.normalization import normalize_company_name


FALLBACK_COMPANY_THUMBNAIL_URL = (
    "https://cdnjs.cloudflare.com/ajax/libs/twemoji/14.0.2/72x72/1f9cc.png"
)


def _truncate(value: object, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _priority_companies(seed_path: str) -> set[str]:
    """Use the curated seed's existing hot-company tier for daily digest flair."""
    try:
        with Path(seed_path).open(encoding="utf-8", newline="") as handle:
            rows = csv.DictReader(handle)
            return {
                normalize_company_name(company)
                for row in rows
                if (company := str(row.get("company") or "").strip())
                and (
                    company.startswith("🔥")
                    or str(row.get("tier") or "").casefold() == "t1"
                    or "big_tech" in str(row.get("categories") or "").casefold()
                )
            }
    except OSError:
        return set()


def _job_highlights(title: str, cycle: str | None, remote_status: str) -> list[str]:
    """Build compact, scannable tags without trying to infer a job's seniority."""
    title_text = title.casefold()
    highlights: list[str] = []
    season_badges = (
        ("spring", "🌸 Spring"),
        ("summer", "☀️ Summer"),
        ("fall", "🍂 Fall"),
        ("winter", "❄️ Winter"),
    )
    cycle_text = (cycle or "").casefold()
    for term, label in season_badges:
        if term in cycle_text:
            highlights.append(label)
            break

    if any(term in title_text for term in ("game developer", "game engineer", "gameplay")):
        highlights.append("🎮 Game Dev")
    elif "data science" in title_text or "data scientist" in title_text:
        highlights.append("📊 Data Science")
    elif any(
        term in title_text
        for term in ("software engineer", "software developer", "software engineering", "swe")
    ):
        highlights.append("💻 SWE")

    if remote_status.casefold() == "remote":
        highlights.append("🛰️ Remote")
    return highlights


class TrackChannelPublisher:
    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config = config
        self.database = database
        self.branding = BrandingResolver(config, database)
        self.client: discord.Client | None = None
        self.logger = logging.getLogger(self.__class__.__name__)
        self._logo_cache: dict[str, str] = {}
        self._priority_companies = _priority_companies(config.company_seed_path)
        # The core feeds and the slower direct-ATS sweep run independently. Keep
        # the stateful pending-delivery claim/send flow single-file so concurrent
        # collectors cannot read the same pending delivery and post it twice.
        self._publish_lock = asyncio.Lock()

    def bind_client(self, client: discord.Client) -> None:
        self.client = client

    async def publish_pending(self) -> tuple[int, int]:
        async with self._publish_lock:
            return await self._publish_pending()

    async def _publish_pending(self) -> tuple[int, int]:
        if self.client is None or self.client.user is None:
            return 0, 0
        sent = 0
        reconciled = 0
        blocked_channels: list[tuple[int, str]] = []
        channel_ids = set(await self.database.list_delivery_channel_ids_for_send())
        channel_ids.add(self.config.discord_channel_id)
        if (
            self.config.discord_new_grad_channel_id is not None
            and self.config.discord_new_grad_channel_id != self.config.discord_channel_id
        ):
            channel_ids.add(self.config.discord_new_grad_channel_id)
        async with AsyncHttpClient(
            timeout_seconds=self.config.request_timeout_seconds,
            retries=self.config.request_retries,
            user_agent=self.config.user_agent,
        ) as http_client:
            for channel_id in sorted(channel_ids):
                try:
                    await self._initialize_digest_cursor(channel_id)
                    channel_sent, channel_reconciled = await self._publish_channel(
                        channel_id,
                        http_client=http_client,
                    )
                    await self._publish_digest_if_due(channel_id)
                except (RuntimeError, discord.HTTPException) as exc:
                    self.logger.error(
                        "delivery_channel_blocked channel=%s error=%s",
                        channel_id,
                        exc,
                    )
                    blocked_channels.append((channel_id, str(exc)))
                    continue
                sent += channel_sent
                reconciled += channel_reconciled
        if blocked_channels:
            summary = "; ".join(
                f"{channel_id}: {error}" for channel_id, error in blocked_channels
            )
            raise RuntimeError(f"Discord delivery blocked for channel(s): {summary}")
        return sent, reconciled

    async def configured_guild_id(self) -> int:
        """Resolve the one Discord server allowed to mutate feed routing."""
        if self.client is None:
            raise RuntimeError("Discord client is not ready")
        channel_ids = {self.config.discord_channel_id}
        if self.config.discord_new_grad_channel_id is not None:
            channel_ids.add(self.config.discord_new_grad_channel_id)
        guild_ids: set[int] = set()
        for channel_id in channel_ids:
            channel = await self._resolve_channel(channel_id)
            guild = getattr(channel, "guild", None)
            guild_id = getattr(guild, "id", None)
            if guild_id is None:
                raise RuntimeError(f"Configured channel {channel_id} is not attached to a server")
            guild_ids.add(int(guild_id))
        if len(guild_ids) != 1:
            raise RuntimeError("Configured job channels must belong to one Discord server")
        return next(iter(guild_ids))

    async def _initialize_digest_cursor(self, channel_id: int) -> None:
        """Avoid summarizing historical posts after a deployment or upgrade."""
        if await self.database.get_digest_cursor(channel_id) is not None:
            return
        await self.database.set_digest_cursor(channel_id, self._now().isoformat())

    async def _publish_digest_if_due(self, channel_id: int) -> None:
        cursor = await self.database.get_digest_cursor(channel_id)
        if cursor is None:  # Defensive fallback if initialization was interrupted.
            await self._initialize_digest_cursor(channel_id)
            return
        try:
            started_at = datetime.fromisoformat(cursor)
        except ValueError:
            self.logger.warning("digest_cursor_invalid channel=%s", channel_id)
            await self.database.set_digest_cursor(channel_id, datetime.now(UTC).isoformat())
            return
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)

        now = self._now()
        scheduled_at = self._daily_digest_at(now)
        if now < scheduled_at or started_at >= scheduled_at:
            return

        deliveries = await self.database.list_sent_deliveries_since(
            channel_id=channel_id,
            sent_after=started_at.isoformat(),
        )
        if not deliveries:
            await self.database.set_digest_cursor(channel_id, now.isoformat())
            return

        channel = await self._resolve_channel(channel_id)
        self.validate_channel_permissions(channel)
        track = str(deliveries[0]["track"])
        if any(str(delivery["track"]) != track for delivery in deliveries):
            raise RuntimeError("A job digest channel must contain one track only")

        marker = self._digest_marker(channel_id, scheduled_at)
        existing_message_id = await self._find_existing_digest_marker(
            channel,
            marker=marker,
            after=scheduled_at - timedelta(minutes=5),
        )
        if existing_message_id is not None:
            await self.database.set_digest_cursor(channel_id, now.isoformat())
            self.logger.info(
                "digest_reconciled channel=%s message=%s", channel_id, existing_message_id
            )
            return

        role_id = self.config.alert_role_id_for_track(track)
        if role_id is not None:
            self.validate_role_mention_permissions(channel)

        message = await channel.send(
            content=f"<@&{role_id}>" if role_id is not None else None,
            embed=self.build_digest_embed(
                deliveries,
                channel=channel,
                marker=marker,
                started_at=started_at,
                scheduled_at=scheduled_at,
            ),
            allowed_mentions=(
                discord.AllowedMentions(roles=[discord.Object(id=role_id)])
                if role_id is not None
                else discord.AllowedMentions.none()
            ),
        )
        await self.database.set_digest_cursor(channel_id, now.isoformat())
        self.logger.info(
            "digest_sent channel=%s track=%s jobs=%s message=%s",
            channel_id,
            track,
            len(deliveries),
            message.id,
        )

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _digest_timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.config.digest_timezone)
        except ZoneInfoNotFoundError:
            self.logger.warning(
                "digest_timezone_invalid timezone=%s; using America/New_York",
                self.config.digest_timezone,
            )
            return ZoneInfo("America/New_York")

    def _daily_digest_at(self, now: datetime) -> datetime:
        local_now = now.astimezone(self._digest_timezone())
        return local_now.replace(
            hour=self.config.digest_hour_local,
            minute=0,
            second=0,
            microsecond=0,
        ).astimezone(UTC)

    async def _publish_channel(
        self,
        channel_id: int,
        *,
        http_client: AsyncHttpClient,
    ) -> tuple[int, int]:
        deliveries = await self.database.list_deliveries_for_send(channel_id)
        if not deliveries:
            return 0, 0

        channel = await self._resolve_channel(channel_id)
        self.validate_channel_permissions(channel)
        existing_markers = await self._load_existing_markers(channel, deliveries)
        sent = 0
        reconciled = 0

        for delivery in deliveries:
            canonical_id = int(delivery["canonical_id"])
            marker = str(delivery["marker"])
            jobs = await self.database.get_canonical_jobs([canonical_id])
            if not jobs:
                continue
            job = jobs[0]
            existing_message_id = existing_markers.get(marker)
            if existing_message_id is not None:
                await self.database.mark_delivery_sent(
                    delivery_id=int(delivery["id"]),
                    canonical_id=canonical_id,
                    track=job.track,
                    channel_id=channel_id,
                    message_id=existing_message_id,
                )
                reconciled += 1
                self.logger.info(
                    "delivery_reconciled delivery=%s canonical=%s channel=%s message=%s",
                    delivery["id"],
                    canonical_id,
                    channel_id,
                    existing_message_id,
                )
                continue

            location_parts = [
                part.strip() for part in job.location.split("|") if part.strip()
            ]
            if not location_parts or not all(
                is_us_location(
                    location,
                    trusted_us=False,
                    context=job.canonical_url,
                )
                for location in location_parts
            ):
                await self.database.mark_delivery_suppressed(
                    int(delivery["id"]),
                    "non-U.S. location rejected by publisher",
                )
                self.logger.warning(
                    "delivery_suppressed_non_us delivery=%s canonical=%s location=%s",
                    delivery["id"],
                    canonical_id,
                    job.location,
                )
                continue

            metadata = await self.database.get_feed_observation_metadata(canonical_id)
            logo_url = await self._resolve_logo_url(
                job,
                metadata=metadata,
                http_client=http_client,
            )
            await self.database.mark_delivery_sending(int(delivery["id"]))
            try:
                message = await channel.send(
                    embed=self.build_embed(
                        job,
                        marker=marker,
                        metadata=metadata,
                        logo_url=logo_url,
                    )
                )
            except (discord.HTTPException, asyncio.TimeoutError) as exc:
                await self.database.mark_delivery_uncertain(int(delivery["id"]), str(exc))
                self.logger.warning(
                    "delivery_uncertain delivery=%s canonical=%s channel=%s company=%r "
                    "title=%r error=%s",
                    delivery["id"],
                    canonical_id,
                    channel_id,
                    job.company,
                    job.title,
                    exc,
                )
                continue

            await self.database.mark_delivery_sent(
                delivery_id=int(delivery["id"]),
                canonical_id=canonical_id,
                track=job.track,
                channel_id=channel_id,
                message_id=int(message.id),
            )
            existing_markers[marker] = int(message.id)
            sent += 1
            self.logger.info(
                "delivery_sent delivery=%s canonical=%s channel=%s company=%r title=%r message=%s",
                delivery["id"],
                canonical_id,
                channel_id,
                job.company,
                job.title,
                message.id,
            )
            if self.config.publish_delay_seconds > 0:
                await asyncio.sleep(self.config.publish_delay_seconds)
        return sent, reconciled

    def build_embed(
        self,
        job: CanonicalJob,
        *,
        marker: str,
        metadata: dict[str, Any],
        logo_url: str | None = None,
    ) -> discord.Embed:
        highlights = _job_highlights(job.title, job.season_term, job.remote_status)
        description_prefix = "🛰️ **REMOTE**\n" if job.remote_status.casefold() == "remote" else ""
        jobright_fallback_url = self._safe_thumbnail_url(metadata.get("jobright_fallback_url"))
        apply_links = (
            f"[Apply directly]({job.canonical_url})\n[Jobright fallback]({jobright_fallback_url})"
            if jobright_fallback_url
            else f"[Apply here]({job.canonical_url})"
        )
        embed = discord.Embed(
            title=_truncate(job.title, 256),
            url=job.canonical_url,
            description=f"{description_prefix}{apply_links}",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Company",
            value=_truncate(job.company or "Unknown", 1_024),
            inline=True,
        )
        embed.add_field(
            name="Location",
            value=_truncate(job.location or "United States", 1_024),
            inline=True,
        )
        embed.add_field(
            name="Track",
            value=job.track.replace("_", " ").title(),
            inline=True,
        )
        embed.add_field(
            name="Cycle",
            value=_truncate(job.season_term or "Not stated", 1_024),
            inline=True,
        )
        if highlights:
            embed.add_field(name="Highlights", value="  •  ".join(highlights), inline=False)
        if metadata.get("posted_date"):
            embed.add_field(name="Posted", value=metadata["posted_date"], inline=True)
        if logo_url:
            embed.set_thumbnail(url=logo_url)
        embed.set_footer(text=f"Job Bot • {marker}")
        return embed

    async def _resolve_logo_url(
        self,
        job: CanonicalJob,
        *,
        metadata: dict[str, Any],
        http_client: AsyncHttpClient,
    ) -> str:
        """Prefer the logo supplied by a source, then cached verified branding."""
        source_logo = self._safe_thumbnail_url(metadata.get("company_logo_url"))
        if source_logo:
            return source_logo

        cache_key = job.normalized_company or job.company.casefold()
        cached = self._logo_cache.get(cache_key)
        if cached:
            return cached
        try:
            resolved = await self.branding.resolve_logo_url_for_job(
                job,
                http_client=http_client,
            )
        except Exception as exc:
            self.logger.debug("company_logo_resolution_failed company=%s error=%s", job.company, exc)
            resolved = None
        logo_url = self._safe_thumbnail_url(resolved) or FALLBACK_COMPANY_THUMBNAIL_URL
        self._logo_cache[cache_key] = logo_url
        return logo_url

    @staticmethod
    def _safe_thumbnail_url(value: object) -> str:
        raw_url = str(value or "").strip()
        return raw_url if sanitize_application_url(raw_url).url else ""

    def build_digest_embed(
        self,
        deliveries: list[Any],
        *,
        channel: Any,
        marker: str,
        started_at: datetime,
        scheduled_at: datetime,
    ) -> discord.Embed:
        track = str(deliveries[0]["track"])
        track_label = track.replace("_", " ").title()
        guild_id = getattr(getattr(channel, "guild", None), "id", None)
        visible_deliveries = deliveries[:12]
        entries = []
        priority_count = 0
        for delivery in visible_deliveries:
            title = _truncate(delivery["title"], 110)
            company = _truncate(delivery["company"], 60)
            location = _truncate(delivery["location"], 70)
            is_priority = normalize_company_name(company) in self._priority_companies
            badge = "🔥 " if is_priority else ""
            priority_count += int(is_priority)
            message_id = delivery["message_id"]
            if guild_id is not None and message_id:
                url = f"https://discord.com/channels/{guild_id}/{channel.id}/{message_id}"
            else:
                url = str(delivery["canonical_url"])
            cycle = _truncate(delivery["season_term"] or "Cycle TBA", 40)
            highlights = _job_highlights(
                str(delivery["title"]),
                delivery["season_term"],
                str(delivery["remote_status"] or ""),
            )
            highlight_text = f"  •  {' · '.join(highlights)}" if highlights else ""
            entries.append(
                f"{badge}[{company} — {title}]({url})\n"
                f"> 📍 {location}  •  {cycle}{highlight_text}"
            )

        hidden_count = len(deliveries) - len(visible_deliveries)
        if hidden_count:
            entries.append(f"*+ {hidden_count} more roles in this drop.*")
        local_zone = self._digest_timezone()
        local_started_at = started_at.astimezone(local_zone)
        local_scheduled_at = scheduled_at.astimezone(local_zone)
        window_start = local_started_at.strftime("%b %d, %-I:%M %p %Z")
        digest_day = local_scheduled_at.strftime("%a, %b %-d")
        highlight = f" · 🔥 {priority_count} priority" if priority_count else ""
        embed = discord.Embed(
            title=f"{track_label} Daily Brief — {digest_day}",
            description=(
                f"**{len(deliveries)} fresh {track_label.casefold()} role(s)**{highlight}\n"
                f"*Since {window_start}*\n\n"
                + "\n\n".join(entries)
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Job Bot • Daily 4 PM ET drop • {marker}")
        return embed

    async def _resolve_channel(self, channel_id: int) -> Any:
        if self.client is None:
            raise RuntimeError("Discord client is not bound")
        channel = self.client.get_channel(channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(channel_id)
        if not hasattr(channel, "send") or not hasattr(channel, "history"):
            raise RuntimeError(f"Configured channel {channel_id} is not a message channel")
        return channel

    def validate_channel_permissions(self, channel: Any, member: Any = None) -> None:
        guild = getattr(channel, "guild", None)
        member = member or (getattr(guild, "me", None) if guild is not None else None)
        if member is None:
            raise RuntimeError("Unable to resolve the bot member for channel permission checks")
        permissions = channel.permissions_for(member)
        required = {
            "view_channel": getattr(permissions, "view_channel", False),
            "embed_links": getattr(permissions, "embed_links", False),
            "read_message_history": getattr(permissions, "read_message_history", False),
        }
        channel_type = getattr(channel, "type", None)
        if channel_type in {
            discord.ChannelType.news_thread,
            discord.ChannelType.private_thread,
            discord.ChannelType.public_thread,
        }:
            required["send_messages_in_threads"] = getattr(
                permissions, "send_messages_in_threads", False
            )
        else:
            required["send_messages"] = getattr(permissions, "send_messages", False)
        missing = [name for name, allowed in required.items() if not allowed]
        if missing:
            raise RuntimeError(
                "Discord channel is missing permissions required for duplicate-safe delivery: "
                + ", ".join(missing)
            )

    def validate_role_mention_permissions(self, channel: Any, member: Any = None) -> None:
        guild = getattr(channel, "guild", None)
        member = member or (getattr(guild, "me", None) if guild is not None else None)
        if member is None:
            raise RuntimeError("Unable to resolve the bot member for role-mention checks")
        permissions = channel.permissions_for(member)
        if not getattr(permissions, "mention_everyone", False):
            raise RuntimeError(
                "Discord channel is missing Mention Everyone permission required for job-alert role pings"
            )

    @staticmethod
    def _digest_marker(channel_id: int, started_at: datetime) -> str:
        raw = f"digest:{channel_id}:{started_at.isoformat()}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
        return f"jobbot:digest:{digest}"

    async def _load_existing_markers(
        self,
        channel: Any,
        deliveries: list[Any],
    ) -> dict[str, int]:
        timestamps = [datetime.fromisoformat(str(row["created_at"])) for row in deliveries]
        after = min(timestamps) - timedelta(minutes=5)
        markers = {str(row["marker"]) for row in deliveries}
        found: dict[str, int] = {}
        try:
            async for message in channel.history(limit=None, after=after, oldest_first=False):
                author = getattr(message, "author", None)
                if self.client is None or author is None or author.id != self.client.user.id:
                    continue
                for embed in getattr(message, "embeds", []):
                    footer = getattr(getattr(embed, "footer", None), "text", None) or ""
                    for marker in markers:
                        if marker in footer:
                            found[marker] = int(message.id)
        except (discord.Forbidden, discord.HTTPException) as exc:
            raise RuntimeError(
                "Unable to reconcile Discord history; refusing to send duplicate-prone messages"
            ) from exc
        return found

    async def _find_existing_digest_marker(
        self,
        channel: Any,
        *,
        marker: str,
        after: datetime,
    ) -> int | None:
        try:
            async for message in channel.history(limit=None, after=after, oldest_first=False):
                author = getattr(message, "author", None)
                if self.client is None or author is None or author.id != self.client.user.id:
                    continue
                for embed in getattr(message, "embeds", []):
                    footer = getattr(getattr(embed, "footer", None), "text", None) or ""
                    if marker in footer:
                        return int(message.id)
        except (discord.Forbidden, discord.HTTPException) as exc:
            raise RuntimeError(
                "Unable to reconcile Discord history; refusing to send duplicate-prone digests"
            ) from exc
        return None
