from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

import discord
from discord.ext import commands

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

