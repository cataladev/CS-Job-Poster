from __future__ import annotations

import discord
import pytest
from discord import app_commands

from job_bot.database import Database
from job_bot.feed_publisher import TrackChannelPublisher
from job_bot.health_commands import (
    _build_deliveries_embed,
    _build_feeds_embed,
    _build_overview_embed,
    register_health_commands,
)


@pytest.mark.asyncio
async def test_health_command_is_registered(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    client = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(client)

    register_health_commands(
        tree,
        database=database,
        config=config,
        publisher=TrackChannelPublisher(config, database),
    )

    command = tree.get_command("health")
    assert command is not None
    assert command.default_permissions is not None
    assert command.default_permissions.administrator
    assert [p.name for p in command.parameters] == ["view"]
    await client.close()
    await database.close()


@pytest.mark.asyncio
async def test_overview_embed_baseline_incomplete(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()

    embed = await _build_overview_embed(database, config)

    assert embed.color == discord.Color.red()
    fields = {f.name: f.value for f in embed.fields}
    assert "Warming up" in fields["Status"]
    assert "In progress" in fields["Baseline"]
    assert "Delivery Queue" in fields
    assert "Database" in fields
    await database.close()


@pytest.mark.asyncio
async def test_overview_embed_baseline_complete(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()

    # Mark baseline as complete
    from datetime import UTC, datetime

    async with database._lock:
        database._connection.execute(
            "INSERT OR REPLACE INTO app_state (key, value, updated_at) VALUES (?, ?, ?)",
            ("initial_feed_baseline_complete", "true", datetime.now(UTC).isoformat()),
        )
        database._connection.commit()

    embed = await _build_overview_embed(database, config)

    assert embed.color == discord.Color.green()
    fields = {f.name: f.value for f in embed.fields}
    assert "healthy" in fields["Status"]
    assert "Complete" in fields["Baseline"]
    await database.close()


@pytest.mark.asyncio
async def test_feeds_embed_no_state(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()

    embed = await _build_feeds_embed(database, config)

    assert embed.description is not None
    assert "no state recorded" in embed.description
    await database.close()


@pytest.mark.asyncio
async def test_feeds_embed_with_healthy_feed(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()

    await database.record_feed_success(
        source_id="simplify_internships",
        repository="SimplifyJobs/Summer2027-Internships",
        path="README.md",
        etag=None,
        content_sha="abc123",
        last_modified=None,
        baseline_complete=True,
    )

    embed = await _build_feeds_embed(database, config)

    assert embed.description is not None
    assert "\u2705" in embed.description  # checkmark
    await database.close()


@pytest.mark.asyncio
async def test_feeds_embed_with_erroring_feed(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()

    await database.record_feed_success(
        source_id="simplify_internships",
        repository="SimplifyJobs/Summer2027-Internships",
        path="README.md",
        etag=None,
        content_sha="abc123",
        last_modified=None,
        baseline_complete=True,
    )
    await database.record_feed_error(
        source_id="simplify_internships",
        repository="SimplifyJobs/Summer2027-Internships",
        path="README.md",
        error="HTTP 403 Forbidden",
    )

    embed = await _build_feeds_embed(database, config)

    assert embed.color == discord.Color.orange()
    assert embed.description is not None
    assert "HTTP 403" in embed.description
    await database.close()


@pytest.mark.asyncio
async def test_deliveries_embed_healthy(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()

    embed = await _build_deliveries_embed(database, config)

    assert embed.color == discord.Color.green()
    fields = {f.name: f.value for f in embed.fields}
    assert "Pending" in fields
    assert "Sent" in fields
    assert "Uncertain" in fields
    await database.close()


@pytest.mark.asyncio
async def test_get_all_feed_states(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()

    await database.record_feed_success(
        source_id="test_feed_1",
        repository="repo/one",
        path="README.md",
        etag=None,
        content_sha="sha1",
        last_modified=None,
    )
    await database.record_feed_success(
        source_id="test_feed_2",
        repository="repo/two",
        path="data.json",
        etag=None,
        content_sha="sha2",
        last_modified=None,
    )

    states = await database.get_all_feed_states()
    source_ids = {str(row["source_id"]) for row in states}
    assert "test_feed_1" in source_ids
    assert "test_feed_2" in source_ids
    await database.close()
