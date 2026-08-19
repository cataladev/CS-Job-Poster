from __future__ import annotations

import logging
from dataclasses import dataclass

import discord
from discord import app_commands

from job_bot.config import (
    RUNTIME_CONFIG_INTERNSHIP_CHANNEL_KEY,
    RUNTIME_CONFIG_INTERNSHIP_ALERT_ROLE_KEY,
    RUNTIME_CONFIG_NEW_GRAD_CHANNEL_KEY,
    RUNTIME_CONFIG_NEW_GRAD_ALERT_ROLE_KEY,
)
from job_bot.database import Database
from job_bot.feed_publisher import TrackChannelPublisher
from job_bot.models import AppConfig


TRACK_CONFIG_KEYS = {
    "internship": RUNTIME_CONFIG_INTERNSHIP_CHANNEL_KEY,
    "new_grad": RUNTIME_CONFIG_NEW_GRAD_CHANNEL_KEY,
}
TRACK_ALERT_ROLE_CONFIG_KEYS = {
    "internship": RUNTIME_CONFIG_INTERNSHIP_ALERT_ROLE_KEY,
    "new_grad": RUNTIME_CONFIG_NEW_GRAD_ALERT_ROLE_KEY,
}
TRACK_LABELS = {
    "internship": "Internship",
    "new_grad": "New-grad",
}
TRACK_CHANNEL_NAMES = {
    "internship": "internship-jobs",
    "new_grad": "new-grad-jobs",
}


@dataclass(frozen=True)
class ChannelConfigurationResult:
    track: str
    channel_id: int
    deliveries_moved: int


@dataclass(frozen=True)
class AlertRoleConfigurationResult:
    track: str
    role_id: int | None


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

    async def configure_alert_role(
        self,
        *,
        track: str,
        role_id: int | None,
        updated_by_user_id: str,
    ) -> AlertRoleConfigurationResult:
        if track not in TRACK_ALERT_ROLE_CONFIG_KEYS:
            raise ValueError("Track must be internship or new_grad.")
        if role_id is not None and role_id <= 0:
            raise ValueError("Role ID must be positive.")

        await self.database.set_runtime_config_value(
            TRACK_ALERT_ROLE_CONFIG_KEYS[track],
            str(role_id) if role_id is not None else None,
            updated_by_user_id,
        )
        if track == "internship":
            self.config.discord_internship_alert_role_id = role_id
        else:
            self.config.discord_new_grad_alert_role_id = role_id
        return AlertRoleConfigurationResult(track=track, role_id=role_id)


def register_job_channel_commands(
    tree: app_commands.CommandTree,
    *,
    configurator: JobChannelConfigurator,
    publisher: TrackChannelPublisher,
) -> None:
    logger = logging.getLogger("job_channel_commands")

    async def require_posting_guild(
        interaction: discord.Interaction,
        guild: discord.Guild,
    ) -> bool:
        try:
            posting_guild_id = await publisher.configured_guild_id()
        except RuntimeError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return False
        if guild.id == posting_guild_id:
            return True
        await interaction.response.send_message(
            "This bot's job feed is bound to a different server. Channel and alert "
            "configuration can only be changed from the established posting server.",
            ephemeral=True,
        )
        logger.warning(
            "cross_guild_job_configuration_blocked attempted_guild=%s posting_guild=%s user=%s",
            guild.id,
            posting_guild_id,
            interaction.user.id,
        )
        return False

    @tree.command(
        name="job-alert",
        description="Opt in or out of the configured internship or new-grad digest alert.",
    )
    @app_commands.guild_only()
    @app_commands.describe(track="Which job digest alert should be toggled?")
    @app_commands.choices(
        track=[
            app_commands.Choice(name="Internships", value="internship"),
            app_commands.Choice(name="New grad", value="new_grad"),
        ]
    )
    async def job_alert(
        interaction: discord.Interaction,
        track: app_commands.Choice[str],
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command must be used in a server.", ephemeral=True
            )
            return
        role_id = configurator.config.alert_role_id_for_track(track.value)
        role = guild.get_role(role_id) if role_id is not None else None
        if role is None:
            await interaction.response.send_message(
                f"No opt-in {TRACK_LABELS[track.value].lower()} alert role is configured yet.",
                ephemeral=True,
            )
            return
        if role.is_default() or role.managed:
            await interaction.response.send_message(
                "That alert role cannot be assigned by the bot. Ask an administrator to configure another role.",
                ephemeral=True,
            )
            return
        bot_member = guild.me
        if bot_member is None and interaction.client.user is not None:
            bot_member = await guild.fetch_member(interaction.client.user.id)
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "I need the Manage Roles permission to manage alert subscriptions.",
                ephemeral=True,
            )
            return
        if role >= bot_member.top_role:
            await interaction.response.send_message(
                "Move my bot role above the alert role, then try again.", ephemeral=True
            )
            return
        member = interaction.user
        if not isinstance(member, discord.Member):
            member = await guild.fetch_member(interaction.user.id)
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Job alert opt-out")
                message = f"You will no longer receive {TRACK_LABELS[track.value].lower()} digest pings."
                action = "opt_out"
            else:
                await member.add_roles(role, reason="Job alert opt-in")
                message = f"You will receive {TRACK_LABELS[track.value].lower()} digest pings."
                action = "opt_in"
        except discord.Forbidden:
            await interaction.response.send_message(
                "Discord blocked that role change. Check my role position and permissions.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(message, ephemeral=True)
        logger.info(
            "job_alert_subscription track=%s action=%s user=%s role=%s",
            track.value,
            action,
            interaction.user.id,
            role.id,
        )

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
        if not await require_posting_guild(interaction, guild):
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

    @tree.command(
        name="create-job-channels",
        description="Create or reuse the standard job channels and configure both tracks.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(category="Optional category that should contain both job channels.")
    async def create_job_channels(
        interaction: discord.Interaction,
        category: discord.CategoryChannel | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command must be used in a server.", ephemeral=True
            )
            return
        if not await require_posting_guild(interaction, guild):
            return
        bot_member = guild.me
        if bot_member is None and interaction.client.user is not None:
            bot_member = await guild.fetch_member(interaction.client.user.id)
        if bot_member is None or not bot_member.guild_permissions.manage_channels:
            await interaction.response.send_message(
                "I need the Manage Channels permission to create the job channels.",
                ephemeral=True,
            )
            return

        channels: dict[str, discord.TextChannel] = {}
        created_tracks: list[str] = []
        for track_name, channel_name in TRACK_CHANNEL_NAMES.items():
            channel = discord.utils.find(
                lambda candidate: candidate.name == channel_name
                and (category is None or candidate.category_id == category.id),
                guild.text_channels,
            )
            if channel is None:
                channel = await guild.create_text_channel(
                    channel_name,
                    category=category,
                    reason=f"Job bot setup requested by {interaction.user}",
                )
                created_tracks.append(track_name)
            try:
                publisher.validate_channel_permissions(channel, bot_member)
            except RuntimeError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            channels[track_name] = channel

        results = []
        for track_name, channel in channels.items():
            result = await configurator.configure(
                track=track_name,
                channel_id=channel.id,
                updated_by_user_id=str(interaction.user.id),
            )
            results.append(result)
        created_label = (
            f" Created: {', '.join(TRACK_LABELS[track] for track in created_tracks)}."
            if created_tracks
            else " Reused the existing standard channels."
        )
        await interaction.response.send_message(
            "Configured "
            + " and ".join(channel.mention for channel in channels.values())
            + "."
            + created_label,
            ephemeral=True,
        )
        logger.info(
            "job_channels_created_or_configured guild=%s user=%s created=%s channels=%s",
            guild.id,
            interaction.user.id,
            created_tracks,
            {result.track: result.channel_id for result in results},
        )

    @create_job_channels.error
    async def create_job_channels_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            message = "Only server administrators can create or configure job channels."
        else:
            logger.exception("create_job_channels_failed", exc_info=error)
            message = "I couldn't create the job channels. Check my channel permissions and try again."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    @tree.command(
        name="set-job-alert-role",
        description="Set or disable the role pinged in periodic job digests.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        track="Which job digest should ping this role?",
        role="Role to ping; leave empty to disable this track's role ping.",
    )
    @app_commands.choices(
        track=[
            app_commands.Choice(name="Internships", value="internship"),
            app_commands.Choice(name="New grad", value="new_grad"),
        ]
    )
    async def set_job_alert_role(
        interaction: discord.Interaction,
        track: app_commands.Choice[str],
        role: discord.Role | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command must be used in a server.", ephemeral=True
            )
            return
        if not await require_posting_guild(interaction, guild):
            return
        if role is not None:
            if role.guild.id != guild.id or role.is_default():
                await interaction.response.send_message(
                    "Choose a non-@everyone role from this server.", ephemeral=True
                )
                return
            channel_id = configurator.config.channel_id_for_track(track.value)
            channel = guild.get_channel(channel_id) if channel_id is not None else None
            bot_member = guild.me
            if channel is not None and bot_member is not None:
                try:
                    publisher.validate_role_mention_permissions(channel, bot_member)
                except RuntimeError as exc:
                    await interaction.response.send_message(str(exc), ephemeral=True)
                    return

        result = await configurator.configure_alert_role(
            track=track.value,
            role_id=role.id if role is not None else None,
            updated_by_user_id=str(interaction.user.id),
        )
        if result.role_id is None:
            message = f"{TRACK_LABELS[result.track]} digests will no longer ping a role."
        else:
            message = (
                f"{TRACK_LABELS[result.track]} digests will ping {role.mention} "
                "at the next digest."
            )
        await interaction.response.send_message(message, ephemeral=True)
        logger.info(
            "job_alert_role_configured track=%s role=%s user=%s",
            result.track,
            result.role_id,
            interaction.user.id,
        )

    @set_job_alert_role.error
    async def set_job_alert_role_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            message = "Only server administrators can change job alert roles."
        else:
            logger.exception("set_job_alert_role_failed", exc_info=error)
            message = "I couldn't update that alert role. Check the bot logs and try again."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
