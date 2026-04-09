import pytest

from job_bot.bot import (
    format_job_tracking_status,
    format_job_visibility,
    parse_jobprocess_input,
    parse_process_command_input,
    normalize_job_tracking_status,
    normalize_job_visibility,
    parse_jobstatus_input,
)
from job_bot.database import Database


@pytest.mark.asyncio
async def test_accept_job_community_rules_defaults_to_matched_visibility(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()

    profile = await database.accept_job_community_rules("123")

    assert profile["user_id"] == "123"
    assert profile["visibility"] == "matched"


def test_process_tracking_normalizers_accept_safe_aliases() -> None:
    assert normalize_job_visibility("public") == "open"
    assert normalize_job_visibility("hidden") == "private"
    assert normalize_job_tracking_status("oa") == "oa_received"
    assert normalize_job_tracking_status("online assessment") == "oa_received"
    assert format_job_visibility("matched") == "Matched Only"
    assert format_job_tracking_status("oa_received") == "OA Received"


def test_parse_jobstatus_input_accepts_pipe_and_space_forms() -> None:
    assert parse_jobstatus_input("nvidia | oa") == ("nvidia", "oa_received")
    assert parse_jobstatus_input("nvidia oa") == ("nvidia", "oa_received")
    assert parse_jobstatus_input("jane street online assessment") == (
        "jane street",
        "oa_received",
    )


def test_parse_jobprocess_input_accepts_notes_and_lookup_forms() -> None:
    assert parse_jobprocess_input("nvidia") == ("nvidia", None, False)
    assert parse_jobprocess_input("nvidia comments") == ("nvidia", None, True)
    assert parse_jobprocess_input("nvidia | oa was chill") == (
        "nvidia",
        "oa was chill",
        False,
    )


def test_parse_process_command_input_routes_common_forms() -> None:
    assert parse_process_command_input("") == ("help", "")
    assert parse_process_command_input("open") == ("visibility", "open")
    assert parse_process_command_input("peers nvidia") == ("jobpeers", "nvidia")
    assert parse_process_command_input("nvidia oa") == ("jobstatus", "nvidia oa")
    assert parse_process_command_input("nvidia notes") == ("jobprocess", "nvidia notes")


@pytest.mark.asyncio
async def test_list_job_application_statuses_for_user_returns_company_rows(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()

    company_id = await database.upsert_watchlist_company(
        company="OpenAI",
        normalized_company="openai",
        careers_url="https://jobs.ashbyhq.com/openai",
        homepage_url="https://openai.com",
        country="US",
        categories=["requested"],
        tier="t0",
        scan_cadence="every_run",
        tracks=["both"],
        seed_source="test",
        notes="",
        active=True,
        next_scan_at=None,
    )
    await database.accept_job_community_rules("123")
    await database.upsert_job_application_status(
        user_id="123",
        company_id=company_id,
        status="applied",
        notes="good recruiter",
    )

    rows = await database.list_job_application_statuses_for_user("123")

    assert len(rows) == 1
    assert rows[0]["company"] == "OpenAI"
    assert rows[0]["status"] == "applied"
    assert rows[0]["notes"] == "good recruiter"


@pytest.mark.asyncio
async def test_job_application_notes_are_preserved_when_status_changes(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()

    company_id = await database.upsert_watchlist_company(
        company="NVIDIA",
        normalized_company="nvidia",
        careers_url="https://nvidia.example.com/careers",
        homepage_url="https://nvidia.example.com",
        country="US",
        categories=["big_tech"],
        tier="t1",
        scan_cadence="every_run",
        tracks=["both"],
        seed_source="test",
        notes="",
        active=True,
        next_scan_at=None,
    )
    await database.accept_job_community_rules("123")
    await database.upsert_job_application_status(
        user_id="123",
        company_id=company_id,
        status="applied",
        notes="oa was mostly lc medium",
    )
    row = await database.upsert_job_application_status(
        user_id="123",
        company_id=company_id,
        status="interview",
    )

    assert row["status"] == "interview"
    assert row["notes"] == "oa was mostly lc medium"


@pytest.mark.asyncio
async def test_job_peers_only_include_allowed_visibility_and_non_hidden_statuses(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()

    company_id = await database.upsert_watchlist_company(
        company="Citadel",
        normalized_company="citadel",
        careers_url="https://www.citadel.com/careers",
        homepage_url="https://www.citadel.com",
        country="US",
        categories=["big_tech"],
        tier="t1",
        scan_cadence="every_run",
        tracks=["both"],
        seed_source="test",
        notes="",
        active=True,
        next_scan_at=None,
    )

    await database.accept_job_community_rules("requester")
    await database.accept_job_community_rules("open-user", default_visibility="matched")
    await database.update_job_community_visibility("open-user", "open")
    await database.accept_job_community_rules("matched-user")
    await database.accept_job_community_rules("private-user")
    await database.update_job_community_visibility("private-user", "private")
    await database.accept_job_community_rules("hidden-user")
    await database.update_job_community_visibility("hidden-user", "open")

    await database.upsert_job_application_status(
        user_id="requester",
        company_id=company_id,
        status="applied",
    )
    await database.upsert_job_application_status(
        user_id="open-user",
        company_id=company_id,
        status="interview",
    )
    await database.upsert_job_application_status(
        user_id="matched-user",
        company_id=company_id,
        status="oa_received",
    )
    await database.upsert_job_application_status(
        user_id="private-user",
        company_id=company_id,
        status="offer",
    )
    await database.upsert_job_application_status(
        user_id="hidden-user",
        company_id=company_id,
        status="hide",
    )

    open_only_rows = await database.list_job_peers(
        company_id=company_id,
        requester_user_id="requester",
        include_matched=False,
        limit=25,
    )
    matched_rows = await database.list_job_peers(
        company_id=company_id,
        requester_user_id="requester",
        include_matched=True,
        limit=25,
    )

    assert [row["user_id"] for row in open_only_rows] == ["open-user"]
    assert [row["user_id"] for row in matched_rows] == ["open-user", "matched-user"]
