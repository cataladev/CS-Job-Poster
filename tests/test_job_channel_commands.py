from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands

from job_bot.config import (
    RUNTIME_CONFIG_INTERNSHIP_ALERT_ROLE_KEY,
    RUNTIME_CONFIG_NEW_GRAD_CHANNEL_KEY,
)
from job_bot.database import Database
from job_bot.feed_publisher import TrackChannelPublisher
from job_bot.github_feeds import JobObservation
from job_bot.job_channel_commands import (
    JobChannelConfigurator,
    register_job_channel_commands,
)
from job_bot.utils.feed_normalization import role_signature
from job_bot.utils.normalization import normalize_job_posting


async def _ingest_new_grad(
    database: Database,
    *,
    source_id: str,
    external_id: str,
    channel_id: int | None,
    suppress: bool,
):
    observation = JobObservation(
        source_id=source_id,
        source_label=source_id,
        source_url=f"https://github.com/{source_id}/jobs",
        external_id=external_id,
        company="Example Corp",
        title="Software Engineer",
        locations=["Austin, TX"],
        application_url="https://jobs.example.com/new-grad-1",
        track="new_grad",
        cycle="2027",
    )
    job = normalize_job_posting(observation.to_job_posting())
    return await database.ingest_feed_job(
        job=job,
        source_id=source_id,
        external_id=external_id,
        source_label=source_id,
        source_url=observation.source_url,
        source_ref="",
        posted_date=None,
        payload=asdict(observation),
        canonical_keys={
            role_signature(
                track=job.track,
                company=job.normalized_company,
                title=job.normalized_title,
                cycle=job.season_term,
                normalized_locations=job.normalized_locations,
            )
        },
        channel_id=channel_id,
        suppress_delivery=suppress,
    )


@pytest.mark.asyncio
async def test_admin_slash_command_is_registered(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)

    register_job_channel_commands(
        tree,
        configurator=JobChannelConfigurator(config, database),
        publisher=TrackChannelPublisher(config, database),
    )

    command = tree.get_command("set-job-channel")
    assert command is not None
    assert command.default_permissions is not None
    assert command.default_permissions.administrator
    assert [parameter.name for parameter in command.parameters] == ["track", "channel"]
    create_channels_command = tree.get_command("create-job-channels")
    assert create_channels_command is not None
    assert create_channels_command.default_permissions is not None
    assert create_channels_command.default_permissions.administrator
    assert [parameter.name for parameter in create_channels_command.parameters] == ["category"]
    alert_role_command = tree.get_command("set-job-alert-role")
    assert alert_role_command is not None
    assert alert_role_command.default_permissions is not None
    assert alert_role_command.default_permissions.administrator
    assert [parameter.name for parameter in alert_role_command.parameters] == ["track", "role"]
    opt_in_command = tree.get_command("job-alert")
    assert opt_in_command is not None
    assert opt_in_command.default_permissions is None
    assert [parameter.name for parameter in opt_in_command.parameters] == ["track"]
    await client.close()
    await database.close()


@pytest.mark.asyncio
async def test_admin_configuration_persists_and_moves_pending_track_delivery(
    config_factory,
) -> None:
    config = config_factory()
    config.discord_new_grad_channel_id = None
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        await _ingest_new_grad(
            database,
            source_id="source-a",
            external_id="grad-a",
            channel_id=222,
            suppress=False,
        )
        configurator = JobChannelConfigurator(config, database)

        result = await configurator.configure(
            track="new_grad",
            channel_id=333,
            updated_by_user_id="42",
        )

        assert result.deliveries_moved == 1
        assert config.discord_new_grad_channel_id == 333
        runtime_row = await database.get_runtime_config_value(
            RUNTIME_CONFIG_NEW_GRAD_CHANNEL_KEY
        )
        assert runtime_row is not None
        assert runtime_row["value"] == "333"
        assert await database.list_deliveries_for_send(222) == []
        assert len(await database.list_deliveries_for_send(333)) == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_alert_role_configuration_persists_and_can_be_disabled(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        configurator = JobChannelConfigurator(config, database)

        enabled = await configurator.configure_alert_role(
            track="internship",
            role_id=555,
            updated_by_user_id="42",
        )
        assert enabled.role_id == 555
        assert config.discord_internship_alert_role_id == 555
        runtime_row = await database.get_runtime_config_value(
            RUNTIME_CONFIG_INTERNSHIP_ALERT_ROLE_KEY
        )
        assert runtime_row is not None
        assert runtime_row["value"] == "555"

        disabled = await configurator.configure_alert_role(
            track="internship",
            role_id=None,
            updated_by_user_id="42",
        )
        assert disabled.role_id is None
        assert config.discord_internship_alert_role_id is None
        runtime_row = await database.get_runtime_config_value(
            RUNTIME_CONFIG_INTERNSHIP_ALERT_ROLE_KEY
        )
        assert runtime_row is not None
        assert runtime_row["value"] is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_member_can_toggle_configured_digest_alert_role(config_factory) -> None:
    class FakeRole:
        def __init__(self, role_id: int, position: int) -> None:
            self.id = role_id
            self.position = position
            self.managed = False

        def is_default(self) -> bool:
            return False

        def __ge__(self, other) -> bool:
            return self.position >= other.position

    class FakeMember:
        def __init__(self) -> None:
            self.roles = []

        async def add_roles(self, role, *, reason):
            self.roles.append(role)

        async def remove_roles(self, role, *, reason):
            self.roles.remove(role)

    class FakeGuild:
        def __init__(self, role, member) -> None:
            self._role = role
            self._member = member
            self.me = SimpleNamespace(
                guild_permissions=SimpleNamespace(manage_roles=True),
                top_role=SimpleNamespace(position=10),
            )

        def get_role(self, role_id):
            return self._role if role_id == self._role.id else None

        async def fetch_member(self, _member_id):
            return self._member

    class FakeResponse:
        def __init__(self) -> None:
            self.messages = []

        async def send_message(self, message, *, ephemeral):
            self.messages.append((message, ephemeral))

    config = config_factory()
    config.discord_internship_alert_role_id = 555
    database = Database(config.sqlite_db_path)
    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    role = FakeRole(555, 1)
    member = FakeMember()
    guild = FakeGuild(role, member)
    response = FakeResponse()
    interaction = SimpleNamespace(
        guild=guild,
        user=SimpleNamespace(id=42),
        response=response,
    )
    try:
        register_job_channel_commands(
            tree,
            configurator=JobChannelConfigurator(config, database),
            publisher=TrackChannelPublisher(config, database),
        )
        command = tree.get_command("job-alert")
        assert command is not None
        choice = app_commands.Choice(name="Internships", value="internship")

        await command.callback(interaction, choice)  # type: ignore[arg-type]
        assert member.roles == [role]
        assert "will receive internship digest pings" in response.messages[-1][0]

        await command.callback(interaction, choice)  # type: ignore[arg-type]
        assert member.roles == []
        assert "will no longer receive internship digest pings" in response.messages[-1][0]
    finally:
        await client.close()
        await database.close()


@pytest.mark.asyncio
async def test_admin_can_create_and_configure_standard_job_channels(config_factory) -> None:
    class FakeChannel:
        def __init__(self, name: str, channel_id: int) -> None:
            self.name = name
            self.id = channel_id
            self.category_id = None
            self.mention = f"#{name}"
            self.permissions = SimpleNamespace(
                view_channel=True,
                send_messages=True,
                embed_links=True,
                read_message_history=True,
            )

        def permissions_for(self, _member):
            return self.permissions

    class FakeGuild:
        def __init__(self) -> None:
            self.id = 123
            self.me = SimpleNamespace(
                guild_permissions=SimpleNamespace(manage_channels=True),
            )
            self.text_channels = []
            self.created = []

        async def create_text_channel(self, name, *, category, reason):
            channel = FakeChannel(name, 700 + len(self.text_channels))
            self.text_channels.append(channel)
            self.created.append((name, category, reason))
            return channel

    class FakeResponse:
        def __init__(self) -> None:
            self.messages = []

        async def send_message(self, message, *, ephemeral):
            self.messages.append((message, ephemeral))

    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)
    guild = FakeGuild()
    response = FakeResponse()
    interaction = SimpleNamespace(
        guild=guild,
        user=SimpleNamespace(id=42),
        response=response,
    )
    try:
        register_job_channel_commands(
            tree,
            configurator=JobChannelConfigurator(config, database),
            publisher=TrackChannelPublisher(config, database),
        )
        command = tree.get_command("create-job-channels")
        assert command is not None

        await command.callback(interaction, None)  # type: ignore[arg-type]

        assert [name for name, _, _ in guild.created] == [
            "internship-jobs",
            "new-grad-jobs",
        ]
        assert config.discord_channel_id == 700
        assert config.discord_new_grad_channel_id == 701
        assert "Configured #internship-jobs and #new-grad-jobs" in response.messages[-1][0]
    finally:
        await client.close()
        await database.close()


@pytest.mark.asyncio
async def test_configuration_rejects_a_shared_track_channel(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        configurator = JobChannelConfigurator(config, database)

        with pytest.raises(ValueError, match="must use different channels"):
            await configurator.configure(
                track="new_grad",
                channel_id=config.discord_channel_id,
                updated_by_user_id="42",
            )

        assert await database.get_runtime_config_value(
            RUNTIME_CONFIG_NEW_GRAD_CHANNEL_KEY
        ) is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_sent_job_is_not_reposted_after_channel_change(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        first = await _ingest_new_grad(
            database,
            source_id="source-a",
            external_id="grad-a",
            channel_id=222,
            suppress=False,
        )
        delivery = (await database.list_deliveries_for_send(222))[0]
        await database.mark_delivery_sent(
            delivery_id=int(delivery["id"]),
            canonical_id=first.canonical_id,
            track="new_grad",
            channel_id=222,
            message_id=999,
        )
        configurator = JobChannelConfigurator(config, database)
        await configurator.configure(
            track="new_grad",
            channel_id=333,
            updated_by_user_id="42",
        )

        duplicate = await _ingest_new_grad(
            database,
            source_id="source-b",
            external_id="grad-b",
            channel_id=333,
            suppress=False,
        )

        assert duplicate.canonical_id == first.canonical_id
        assert await database.list_delivery_channel_ids_for_send() == []
        assert await database.get_delivery_counts() == {"sent": 1}
    finally:
        await database.close()
