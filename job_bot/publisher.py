from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional
from urllib.parse import urlencode, urlparse

import discord
from discord.ext import commands
from rapidfuzz import fuzz

from job_bot.database import Database
from job_bot.models import AppConfig, CanonicalJob
from job_bot.utils.html import collapse_whitespace, strip_html


SUMMARY_LIMIT = 140
ROLE_SIGNAL_TERMS = (
    "engineer",
    "intern",
    "team",
    "build",
    "platform",
    "backend",
    "frontend",
    "ml",
    "ai",
)
BOILERPLATE_TERMS = (
    "leading the industry",
    "cutting-edge",
    "revolutionizing",
    "redefining",
    "mission",
    "performance expectations",
    "ease of use",
    "cost efficiency",
)
SHARED_HOST_SUFFIXES = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "oraclecloud.com",
    "icims.com",
    "applytojob.com",
    "workatastartup.com",
    "simplify.jobs",
)


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
            logo_url = await self._resolve_logo_url(job)
            message = await channel.send(embed=self._build_embed(job, logo_url=logo_url))
            await self.database.mark_posted(job.id, job.track, channel_id, message.id)
            posted_ids.append(job.id)
            await asyncio.sleep(self.config.publish_delay_seconds)
        return posted_ids

    def _build_embed(self, job: CanonicalJob, *, logo_url: Optional[str] = None) -> discord.Embed:
        embed = discord.Embed(
            title=job.title,
            url=job.canonical_url,
            description=self._compact_summary(job.summary),
            color=discord.Color.blue(),
        )
        embed.add_field(name="Company", value=job.company or "Unknown", inline=True)
        embed.add_field(name="Location", value=job.location or "Unknown", inline=True)
        embed.add_field(name="Track", value=job.track.replace("_", " ").title(), inline=True)
        embed.add_field(name="Source(s)", value=", ".join(job.source_names) or "Unknown", inline=False)
        if job.provenance_tags:
            embed.add_field(
                name="Watchlist",
                value=", ".join(job.provenance_tags),
                inline=False,
            )
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
        if logo_url:
            embed.set_thumbnail(url=logo_url)
        embed.set_footer(text="Internship Aggregator Bot")
        return embed

    async def _resolve_logo_url(self, job: CanonicalJob) -> Optional[str]:
        if not self.database or not job.normalized_company:
            return None
        branding = await self.database.get_company_branding(job.normalized_company)
        if not branding:
            return None
        return self._build_logo_thumbnail_url(
            homepage_url=branding["homepage_url"] or "",
            careers_url=branding["careers_url"] or "",
        )

    def _compact_summary(self, summary: str) -> str:
        cleaned = collapse_whitespace(strip_html(summary))
        if not cleaned:
            return "Open listing for details."

        sentences = [
            part.strip(" -")
            for part in re.split(r"(?<=[.!?])\s+|\s+\|\s+", cleaned)
            if part.strip(" -")
        ]
        if not sentences:
            sentences = [cleaned]

        def score(sentence: str) -> tuple[int, int]:
            lowered = sentence.lower()
            role_hits = sum(term in lowered for term in ROLE_SIGNAL_TERMS)
            boilerplate_hits = sum(term in lowered for term in BOILERPLATE_TERMS)
            length_bonus = 1 if 25 <= len(sentence) <= SUMMARY_LIMIT else 0
            return (role_hits * 4 + length_bonus - boilerplate_hits * 5, -len(sentence))

        chosen = max(sentences, key=score)
        if score(chosen)[0] < 0:
            non_boilerplate = [
                sentence
                for sentence in sentences
                if not any(term in sentence.lower() for term in BOILERPLATE_TERMS)
            ]
            chosen = non_boilerplate[0] if non_boilerplate else ""

        chosen = collapse_whitespace(chosen)
        if not chosen:
            return "Open listing for details."
        if len(chosen) <= SUMMARY_LIMIT:
            return chosen
        trimmed = chosen[: SUMMARY_LIMIT - 1].rstrip(" ,;:-")
        return f"{trimmed}…"

    def _build_logo_thumbnail_url(
        self,
        *,
        homepage_url: str,
        careers_url: str,
    ) -> Optional[str]:
        for candidate in (homepage_url, careers_url):
            domain_url = self._extract_brand_domain(candidate)
            if domain_url:
                return "https://www.google.com/s2/favicons?" + urlencode(
                    {"sz": "128", "domain_url": domain_url}
                )
        return None

    def _extract_brand_domain(self, url: str) -> Optional[str]:
        if not url:
            return None
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return None
        host = parsed.netloc.lower()
        if any(host == suffix or host.endswith(f".{suffix}") for suffix in SHARED_HOST_SUFFIXES):
            return None
        return f"{parsed.scheme.lower()}://{host}"

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
