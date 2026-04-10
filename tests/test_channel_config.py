from types import SimpleNamespace

import discord
import pytest

from job_bot.bot import (
    build_channel_configuration_message,
    parse_channel_command_value,
    validate_configurable_channel,
)
from job_bot.config import (
    RUNTIME_CONFIG_INTERNSHIP_CHANNEL_KEY,
    RUNTIME_CONFIG_NEW_GRAD_CHANNEL_KEY,
    apply_runtime_channel_overrides,
)
from job_bot.database import Database


class DummyChannel:
    def __init__(self, permissions, *, channel_type=discord.ChannelType.text):
        self.guild = object()
        self.type = channel_type
        self._permissions = permissions

    async def send(self, *args, **kwargs):
        return None

    def permissions_for(self, bot_member):
        return self._permissions


def test_apply_runtime_channel_overrides_leave_env_defaults_when_absent(config_factory) -> None:
    config = config_factory()

    apply_runtime_channel_overrides(config, {})

    assert config.discord_channel_id == 111
    assert config.discord_new_grad_channel_id == 222


def test_apply_runtime_channel_overrides_update_channel_routing(config_factory) -> None:
    config = config_factory()

    apply_runtime_channel_overrides(
        config,
        {
            RUNTIME_CONFIG_INTERNSHIP_CHANNEL_KEY: {"value": "333"},
            RUNTIME_CONFIG_NEW_GRAD_CHANNEL_KEY: {"value": None},
        },
    )

    assert config.discord_channel_id == 333
    assert config.discord_new_grad_channel_id is None


def test_build_channel_configuration_message_reports_effective_sources(config_factory) -> None:
    config = config_factory()
    config.discord_channel_id = 333
    config.discord_new_grad_channel_id = None

    message = build_channel_configuration_message(
        config=config,
        runtime_config={
            RUNTIME_CONFIG_INTERNSHIP_CHANNEL_KEY: {"value": "333"},
            RUNTIME_CONFIG_NEW_GRAD_CHANNEL_KEY: {"value": None},
        },
        startup_channel_defaults={
            RUNTIME_CONFIG_INTERNSHIP_CHANNEL_KEY: 111,
            RUNTIME_CONFIG_NEW_GRAD_CHANNEL_KEY: 222,
        },
        startup_new_grad_collection_enabled=False,
    )

    assert "Internship: `333` (runtime override; startup default `111`)" in message
    assert "New grad: `disabled` (runtime override; startup default `222`)" in message
    assert "New-grad collection at startup: `disabled`" in message


def test_parse_channel_command_value_accepts_numeric_ids_and_off() -> None:
    assert parse_channel_command_value("123456") == 123456
    assert parse_channel_command_value("off", allow_off=True) is None


def test_parse_channel_command_value_rejects_invalid_tokens() -> None:
    with pytest.raises(ValueError, match="numeric channel ID"):
        parse_channel_command_value("abc")


def test_validate_configurable_channel_rejects_missing_permissions() -> None:
    channel = DummyChannel(
        SimpleNamespace(
            view_channel=True,
            send_messages=True,
            send_messages_in_threads=False,
            embed_links=False,
        )
    )

    assert validate_configurable_channel(channel, object()) == "I can't embed links in that channel."


@pytest.mark.asyncio
async def test_runtime_config_round_trip_in_sqlite(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()

    try:
        assert await database.get_runtime_config_value(RUNTIME_CONFIG_INTERNSHIP_CHANNEL_KEY) is None

        internship_row = await database.set_runtime_config_value(
            RUNTIME_CONFIG_INTERNSHIP_CHANNEL_KEY,
            "333",
            "42",
        )
        new_grad_row = await database.set_runtime_config_value(
            RUNTIME_CONFIG_NEW_GRAD_CHANNEL_KEY,
            None,
            "42",
        )

        assert internship_row["value"] == "333"
        assert new_grad_row["value"] is None

        runtime_rows = await database.list_runtime_config()
        assert {row["key"]: row["value"] for row in runtime_rows} == {
            RUNTIME_CONFIG_INTERNSHIP_CHANNEL_KEY: "333",
            RUNTIME_CONFIG_NEW_GRAD_CHANNEL_KEY: None,
        }
    finally:
        await database.close()
