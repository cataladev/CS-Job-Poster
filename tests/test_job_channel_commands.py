from __future__ import annotations

from dataclasses import asdict

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
    alert_role_command = tree.get_command("set-job-alert-role")
    assert alert_role_command is not None
    assert alert_role_command.default_permissions is not None
    assert alert_role_command.default_permissions.administrator
    assert [parameter.name for parameter in alert_role_command.parameters] == ["track", "role"]
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
