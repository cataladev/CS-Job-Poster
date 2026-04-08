from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

import discord
from discord.ext import commands
from rapidfuzz import fuzz

from job_bot.database import Database
from job_bot.models import AppConfig, CanonicalJob


class DiscordPublisher:
    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config = config
        self.database = database
        self.bot: Optional[commands.Bot] = None
        self.logger = logging.getLogger(self.__class__.__name__)

    def bind_bot(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def publish_jobs(self, jobs: List[CanonicalJob]) -> List[int]:
        if not self.bot:
            return []

        await self.bot.wait_until_ready()
        posted_ids: List[int] = []
        for job in jobs:
            if job.status != "active" or job.id is None:
                continue
            channel_id = self.config.channel_id_for_track(job.track)
            if not channel_id:
                continue
            if await self.database.is_posted(job.id, channel_id):
                continue
            if await self._is_probable_repeat_for_channel(job, channel_id):
                self.logger.info(
                    "Skipping probable repeat for %s - %s in channel %s",
                    job.company,
                    job.title,
                    channel_id,
                )
                continue
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            message = await channel.send(embed=self._build_embed(job))
            await self.database.mark_posted(job.id, job.track, channel_id, message.id)
            posted_ids.append(job.id)
            await asyncio.sleep(self.config.publish_delay_seconds)
        return posted_ids

    def _build_embed(self, job: CanonicalJob) -> discord.Embed:
        embed = discord.Embed(
            title=job.title,
            url=job.canonical_url,
            description=job.summary or "No summary available.",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Company", value=job.company or "Unknown", inline=True)
        embed.add_field(name="Location", value=job.location or "Unknown", inline=True)
        embed.add_field(name="Track", value=job.track.replace("_", " ").title(), inline=True)
        embed.add_field(name="Source(s)", value=", ".join(job.source_names) or "Unknown", inline=False)
        embed.add_field(
            name="Season",
            value=job.season_term or "Unknown",
            inline=True,
        )
        embed.add_field(
            name="Date Found",
            value=job.first_seen.strftime("%Y-%m-%d"),
            inline=True,
        )
        embed.set_footer(text="Internship Aggregator Bot")
        return embed

    async def _is_probable_repeat_for_channel(self, job: CanonicalJob, channel_id: int) -> bool:
        candidates = await self.database.get_posted_candidates(
            channel_id=channel_id,
            track=job.track,
            normalized_company=job.normalized_company,
        )
        for candidate in candidates:
            if candidate.id == job.id:
                continue
            if self._is_probable_repeat(job, candidate):
                return True
        return False

    def _is_probable_repeat(self, job: CanonicalJob, candidate: CanonicalJob) -> bool:
        if job.track != candidate.track:
            return False
        if job.normalized_company != candidate.normalized_company:
            return False
        if job.canonical_url == candidate.canonical_url:
            return True

        title_score = fuzz.token_set_ratio(job.normalized_title, candidate.normalized_title)
        location_score = fuzz.token_set_ratio(
            " ".join(job.normalized_locations),
            " ".join(candidate.normalized_locations),
        )
        same_season = (job.season_term or "") == (candidate.season_term or "")

        if same_season and title_score >= 95:
            return True
        if same_season and title_score >= 90 and location_score >= 85:
            return True
        if title_score >= 98 and location_score >= 92:
            return True
        return False
