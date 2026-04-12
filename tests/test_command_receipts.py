from __future__ import annotations

from types import SimpleNamespace

import pytest

from job_bot.bot import build_command_receipt_payload, build_runtime_instance_id
from job_bot.database import Database


def test_build_runtime_instance_id_uses_hostname_and_pid() -> None:
    assert build_runtime_instance_id(hostname="vps-f77add1e", pid=1234) == "vps-f77add1e:1234"


def test_build_command_receipt_payload_extracts_command_metadata() -> None:
    ctx = SimpleNamespace(
        message=SimpleNamespace(id=123),
        command=SimpleNamespace(qualified_name="help"),
        author=SimpleNamespace(id=456),
        channel=SimpleNamespace(id=789),
        guild=SimpleNamespace(id=321),
    )

    assert build_command_receipt_payload(ctx) == {
        "message_id": "123",
        "command_name": "help",
        "author_user_id": "456",
        "channel_id": "789",
        "guild_id": "321",
    }


def test_build_command_receipt_payload_returns_none_without_command() -> None:
    ctx = SimpleNamespace(
        message=SimpleNamespace(id=123),
        command=None,
        author=SimpleNamespace(id=456),
        channel=SimpleNamespace(id=789),
        guild=None,
    )

    assert build_command_receipt_payload(ctx) is None


@pytest.mark.asyncio
async def test_claim_command_receipt_is_idempotent_in_sqlite(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()

    try:
        assert await database.claim_command_receipt(
            message_id="1001",
            command_name="help",
            author_user_id="42",
            channel_id="99",
            guild_id="77",
            instance_id="instance-a",
        )
        assert not await database.claim_command_receipt(
            message_id="1001",
            command_name="help",
            author_user_id="42",
            channel_id="99",
            guild_id="77",
            instance_id="instance-b",
        )
        assert await database.claim_command_receipt(
            message_id="1002",
            command_name="ping",
            author_user_id="42",
            channel_id="99",
            guild_id=None,
            instance_id="instance-b",
        )
    finally:
        await database.close()
