from __future__ import annotations

import logging
from dataclasses import dataclass

import discord
from discord import app_commands

from job_bot.config import (
    RUNTIME_CONFIG_INTERNSHIP_CHANNEL_KEY,
    RUNTIME_CONFIG_NEW_GRAD_CHANNEL_KEY,
)
from job_bot.database import Database
from job_bot.feed_publisher import TrackChannelPublisher
from job_bot.models import AppConfig


TRACK_CONFIG_KEYS = {
    "internship": RUNTIME_CONFIG_INTERNSHIP_CHANNEL_KEY,
    "new_grad": RUNTIME_CONFIG_NEW_GRAD_CHANNEL_KEY,
}
TRACK_LABELS = {
    "internship": "Internship",
    "new_grad": "New-grad",
}


@dataclass(frozen=True)
class ChannelConfigurationResult:
    track: str
    channel_id: int
    deliveries_moved: int


class JobChannelConfigurator:
    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config = config
        self.database = database

    async def configure(
        self,
        *,
        track: str,
        channel_id: int,
        updated_by_user_id: str,
    ) -> ChannelConfigurationResult:
        if track not in TRACK_CONFIG_KEYS:
            raise ValueError("Track must be internship or new_grad.")
        if channel_id <= 0:
            raise ValueError("Channel ID must be positive.")

        other_channel_id = (
            self.config.discord_new_grad_channel_id
            if track == "internship"
            else self.config.discord_channel_id
        )
        if other_channel_id == channel_id:
            raise ValueError(
                "Internship and new-grad jobs must use different channels."
            )

        await self.database.set_runtime_config_value(
            TRACK_CONFIG_KEYS[track],
            str(channel_id),
            updated_by_user_id,
        )
        if track == "internship":
            self.config.discord_channel_id = channel_id
        else:
            self.config.discord_new_grad_channel_id = channel_id

        moved = await self.database.reassign_unsent_deliveries_for_track(
            track=track,
            channel_id=channel_id,
        )
        return ChannelConfigurationResult(
            track=track,
            channel_id=channel_id,
            deliveries_moved=moved,
        )


def register_job_channel_commands(
    tree: app_commands.CommandTree,
    *,
    configurator: JobChannelConfigurator,
    publisher: TrackChannelPublisher,
) -> None:
    logger = logging.getLogger("job_channel_commands")

    @tree.command(
        name="set-job-channel",
        description="Set the Discord channel for internship or new-grad job posts.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        track="Which job feed should use this channel?",
        channel="The channel where new jobs should be posted.",
    )
    @app_commands.choices(
        track=[
            app_commands.Choice(name="Internships", value="internship"),
            app_commands.Choice(name="New grad", value="new_grad"),
        ]
    )
    async def set_job_channel(
        interaction: discord.Interaction,
        track: app_commands.Choice[str],
        channel: discord.TextChannel,
    ) -> None:
        guild = interaction.guild
        if guild is None or channel.guild.id != guild.id:
            await interaction.response.send_message(
                "Choose a text channel from this server.",
                ephemeral=True,
            )
            return

        bot_member = guild.me
        if bot_member is None and interaction.client.user is not None:
            bot_member = await guild.fetch_member(interaction.client.user.id)
        if bot_member is None:
            await interaction.response.send_message(
                "I couldn't verify my permissions in that channel.",
                ephemeral=True,
            )
            return

        try:
            publisher.validate_channel_permissions(channel, bot_member)
            result = await configurator.configure(
                track=track.value,
                channel_id=channel.id,
                updated_by_user_id=str(interaction.user.id),
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except RuntimeError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.send_message(
            f"{TRACK_LABELS[result.track]} jobs will now post in {channel.mention}. "
            "Previously sent jobs will not be reposted.",
            ephemeral=True,
        )
        logger.info(
            "job_channel_configured track=%s channel=%s user=%s deliveries_moved=%s",
            result.track,
            result.channel_id,
            interaction.user.id,
            result.deliveries_moved,
        )

    @set_job_channel.error
    async def set_job_channel_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            message = "Only server administrators can change job channels."
        else:
            logger.exception("set_job_channel_failed", exc_info=error)
            message = "I couldn't update that channel. Check the bot logs and try again."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
