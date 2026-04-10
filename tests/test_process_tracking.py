from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from job_bot.bot import (
    format_job_tracking_status,
    format_job_visibility,
    normalize_job_tracking_status,
    normalize_job_visibility,
    parse_jobstatus_input,
    parse_process_command_input,
    should_dm_process_reply,
)
from job_bot.database import Database
from job_bot.process_tracking import (
    build_process_next_action,
    normalize_process_track,
    normalize_role_title,
    normalize_season_term,
    parse_process_note_command,
    parse_process_status_command,
    validate_process_note,
)


async def _create_company(
    database: Database,
    *,
    company: str = "OpenAI",
    normalized_company: str = "openai",
    careers_url: str = "https://jobs.ashbyhq.com/openai",
    homepage_url: str = "https://openai.com",
) -> int:
    return await database.upsert_watchlist_company(
        company=company,
        normalized_company=normalized_company,
        careers_url=careers_url,
        homepage_url=homepage_url,
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


async def _create_scope(
    database: Database,
    *,
    company_id: int,
    company: str = "OpenAI",
    role_title: str = "Software Engineer Intern",
    track: str = "internship",
    season_term: str = "Summer 2027",
) -> object:
    return await database.upsert_process_role_scope(
        company_id=company_id,
        canonical_job_id=None,
        company=company,
        normalized_company=company.lower(),
        role_title=role_title,
        normalized_role_title=normalize_role_title(role_title),
        track=track,
        season_term=season_term,
        canonical_job_url="",
        scope_origin="manual",
    )


@pytest.mark.asyncio
async def test_accept_job_community_rules_defaults_to_private_visibility(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()

    profile = await database.accept_job_community_rules("123")

    assert profile["user_id"] == "123"
    assert profile["visibility"] == "private"


def test_process_reply_routing_respects_open_visibility() -> None:
    assert should_dm_process_reply(in_guild=False, visibility="private") is False
    assert should_dm_process_reply(in_guild=True, visibility=None) is True
    assert should_dm_process_reply(in_guild=True, visibility="private") is True
    assert should_dm_process_reply(in_guild=True, visibility="matched") is True
    assert should_dm_process_reply(in_guild=True, visibility="open") is False
    assert should_dm_process_reply(in_guild=True, visibility="open", force_dm=True) is True


def test_process_tracking_normalizers_parsers_and_next_action() -> None:
    assert normalize_job_visibility("public") == "open"
    assert normalize_job_visibility("hidden") == "private"
    assert normalize_job_tracking_status("online assessment") == "oa_received"
    assert format_job_visibility("matched") == "Matched Only"
    assert format_job_tracking_status("oa_received") == "OA Received"
    assert normalize_process_track("new grad") == "new_grad"
    assert normalize_season_term("summer 2027") == "Summer 2027"
    assert normalize_role_title("Software Engineer Intern (ML)") == "software engineer intern ml"

    assert parse_jobstatus_input("12 oa") == ("12", "oa_received")
    assert parse_process_status_command(
        "Stripe | Software Engineer Intern | Summer 2027 | applied"
    ) == ("Stripe", "Software Engineer Intern", "Summer 2027", "applied", None)
    assert parse_process_status_command(
        "Stripe | Software Engineer | new grad | Fall 2027 | offer"
    ) == ("Stripe", "Software Engineer", "Fall 2027", "offer", "new_grad")
    assert parse_process_note_command("12 | recruiter said 2 rounds") == (
        "12",
        "recruiter said 2 rounds",
    )

    assert parse_process_command_input("") == ("dashboard", "")
    assert parse_process_command_input("open") == ("visibility", "open")
    assert parse_process_command_input("set Stripe | SWE Intern | Summer 2027 | applied") == (
        "jobstatus",
        "Stripe | SWE Intern | Summer 2027 | applied",
    )
    assert parse_process_command_input("peers 12") == ("jobpeers", "12")
    assert parse_process_command_input("notes 12") == ("processnotes", "12")
    assert parse_process_command_input("12 oa") == ("jobstatus", "12 oa")

    stale_applied_at = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    assert "Reach out" in build_process_next_action("applied", stale_applied_at)

    assert validate_process_note("recruiter said 2 rounds") is None
    assert validate_process_note("email me at test@example.com") == "Notes cannot include email addresses."
    assert validate_process_note("https://example.com") == "Notes cannot include links or attachments."
    assert (
        validate_process_note("sharing interview question bank")
        == "Notes cannot include leaked or private process content."
    )


@pytest.mark.asyncio
async def test_role_statuses_are_split_by_track_and_season(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()

    company_id = await _create_company(database)
    await database.accept_job_community_rules("123")
    internship_scope = await _create_scope(
        database,
        company_id=company_id,
        role_title="Software Engineer Intern",
        track="internship",
        season_term="Summer 2027",
    )
    new_grad_scope = await _create_scope(
        database,
        company_id=company_id,
        role_title="Software Engineer",
        track="new_grad",
        season_term="Fall 2027",
    )

    internship_row = await database.upsert_process_role_status(
        user_id="123",
        scope_id=int(internship_scope["id"]),
        status="applied",
    )
    new_grad_row = await database.upsert_process_role_status(
        user_id="123",
        scope_id=int(new_grad_scope["id"]),
        status="interview",
    )

    rows = await database.list_process_role_statuses_for_user("123", active_only=True, limit=25)
    scoped_matches = await database.find_process_role_scopes(
        normalized_company="openai",
        normalized_role_title=normalize_role_title("Software Engineer Intern"),
        season_term="Summer 2027",
        track="internship",
        limit=10,
    )

    assert {row["scope_id"] for row in rows} == {int(internship_scope["id"]), int(new_grad_scope["id"])}
    assert internship_row["track"] == "internship"
    assert new_grad_row["track"] == "new_grad"
    assert internship_row["season_term"] == "Summer 2027"
    assert new_grad_row["season_term"] == "Fall 2027"
    assert [row["id"] for row in scoped_matches] == [int(internship_scope["id"])]


@pytest.mark.asyncio
async def test_role_notes_are_preserved_as_history_for_each_scope(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()

    company_id = await _create_company(database, company="NVIDIA", normalized_company="nvidia")
    await database.accept_job_community_rules("123")
    scope = await _create_scope(
        database,
        company_id=company_id,
        company="NVIDIA",
        role_title="CUDA Software Intern",
        track="internship",
        season_term="Summer 2027",
    )
    status_row = await database.upsert_process_role_status(
        user_id="123",
        scope_id=int(scope["id"]),
        status="applied",
    )
    await database.add_process_role_note(
        status_id=int(status_row["id"]),
        scope_id=int(scope["id"]),
        user_id="123",
        body="OA was mostly arrays and graphs.",
    )
    await database.add_process_role_note(
        status_id=int(status_row["id"]),
        scope_id=int(scope["id"]),
        user_id="123",
        body="Recruiter said final round is next week.",
    )

    updated_status = await database.get_process_role_status(user_id="123", scope_id=int(scope["id"]))
    notes = await database.list_process_role_notes(
        scope_id=int(scope["id"]),
        requester_user_id="123",
        include_matched=True,
        limit=25,
    )

    assert updated_status["note_count"] == 2
    assert updated_status["latest_note"] == "Recruiter said final round is next week."
    assert [note["body"] for note in notes] == [
        "Recruiter said final round is next week.",
        "OA was mostly arrays and graphs.",
    ]


@pytest.mark.asyncio
async def test_role_peers_and_notes_respect_visibility_rules(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()

    company_id = await _create_company(database, company="Ramp", normalized_company="ramp")
    scope = await _create_scope(
        database,
        company_id=company_id,
        company="Ramp",
        role_title="Backend Engineer Intern",
        track="internship",
        season_term="Summer 2027",
    )

    await database.accept_job_community_rules("requester")
    await database.accept_job_community_rules("open-user")
    await database.update_job_community_visibility("open-user", "open")
    await database.accept_job_community_rules("matched-user", default_visibility="matched")
    await database.accept_job_community_rules("private-user")
    await database.update_job_community_visibility("private-user", "private")
    await database.accept_job_community_rules("hidden-user")
    await database.update_job_community_visibility("hidden-user", "open")

    requester_status = await database.upsert_process_role_status(
        user_id="requester",
        scope_id=int(scope["id"]),
        status="applied",
    )
    open_status = await database.upsert_process_role_status(
        user_id="open-user",
        scope_id=int(scope["id"]),
        status="interview",
    )
    matched_status = await database.upsert_process_role_status(
        user_id="matched-user",
        scope_id=int(scope["id"]),
        status="oa_received",
    )
    private_status = await database.upsert_process_role_status(
        user_id="private-user",
        scope_id=int(scope["id"]),
        status="offer",
    )
    hidden_status = await database.upsert_process_role_status(
        user_id="hidden-user",
        scope_id=int(scope["id"]),
        status="hide",
    )

    await database.add_process_role_note(
        status_id=int(open_status["id"]),
        scope_id=int(scope["id"]),
        user_id="open-user",
        body="Practical interview focused on APIs.",
    )
    await database.add_process_role_note(
        status_id=int(matched_status["id"]),
        scope_id=int(scope["id"]),
        user_id="matched-user",
        body="Got the OA after two weeks.",
    )
    await database.add_process_role_note(
        status_id=int(requester_status["id"]),
        scope_id=int(scope["id"]),
        user_id="requester",
        body="Recruiter replied same day.",
    )
    await database.add_process_role_note(
        status_id=int(private_status["id"]),
        scope_id=int(scope["id"]),
        user_id="private-user",
        body="Timeline moved fast.",
    )
    await database.add_process_role_note(
        status_id=int(hidden_status["id"]),
        scope_id=int(scope["id"]),
        user_id="hidden-user",
        body="Should stay hidden.",
    )

    open_only_peers = await database.list_process_role_peers(
        scope_id=int(scope["id"]),
        requester_user_id="requester",
        include_matched=False,
        limit=25,
    )
    matched_peers = await database.list_process_role_peers(
        scope_id=int(scope["id"]),
        requester_user_id="requester",
        include_matched=True,
        limit=25,
    )
    open_only_notes = await database.list_process_role_notes(
        scope_id=int(scope["id"]),
        requester_user_id="requester",
        include_matched=False,
        limit=25,
    )
    matched_notes = await database.list_process_role_notes(
        scope_id=int(scope["id"]),
        requester_user_id="requester",
        include_matched=True,
        limit=25,
    )

    assert [row["user_id"] for row in open_only_peers] == ["open-user"]
    assert [row["user_id"] for row in matched_peers] == ["open-user", "matched-user"]
    assert [row["user_id"] for row in open_only_notes] == ["requester", "open-user"]
    assert [row["user_id"] for row in matched_notes] == [
        "requester",
        "matched-user",
        "open-user",
    ]


@pytest.mark.asyncio
async def test_reporting_and_deleting_role_notes_moderates_content(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()

    company_id = await _create_company(database, company="Stripe", normalized_company="stripe")
    scope = await _create_scope(
        database,
        company_id=company_id,
        company="Stripe",
        role_title="Software Engineer Intern",
        track="internship",
        season_term="Summer 2027",
    )
    for user_id, visibility in (
        ("author", "private"),
        ("reporter", "open"),
        ("second-reporter", "open"),
    ):
        await database.accept_job_community_rules(user_id, default_visibility=visibility)

    author_status = await database.upsert_process_role_status(
        user_id="author",
        scope_id=int(scope["id"]),
        status="interview",
    )
    note = await database.add_process_role_note(
        status_id=int(author_status["id"]),
        scope_id=int(scope["id"]),
        user_id="author",
        body="This process note stays visible until enough people report it.",
    )

    once_reported = await database.report_process_role_note(
        note_id=int(note["id"]),
        reporter_user_id="reporter",
        reason="possible leak",
        auto_hide_threshold=2,
    )
    twice_reported = await database.report_process_role_note(
        note_id=int(note["id"]),
        reporter_user_id="second-reporter",
        reason="definitely leaked",
        auto_hide_threshold=2,
    )
    unauthorized_delete = await database.delete_process_role_note(
        note_id=int(note["id"]),
        actor_user_id="reporter",
        allow_moderator_delete=False,
        reason="",
    )
    deleted = await database.delete_process_role_note(
        note_id=int(note["id"]),
        actor_user_id="author",
        allow_moderator_delete=False,
        reason="cleanup",
    )

    assert once_reported["report_count"] == 1
    assert once_reported["moderation_status"] == "active"
    assert twice_reported["report_count"] == 2
    assert twice_reported["moderation_status"] == "hidden"
    assert unauthorized_delete is None
    assert deleted["moderation_status"] == "deleted"
    assert deleted["deleted_by_user_id"] == "author"


@pytest.mark.asyncio
async def test_process_activity_rate_limit_storage(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()

    await database.accept_job_community_rules("123")
    await database.record_process_activity(user_id="123", event_type="note_create", metadata={"note_id": 1})
    await database.record_process_activity(user_id="123", event_type="note_create", metadata={"note_id": 2})
    await database.record_process_activity(user_id="123", event_type="status_update", metadata={"status": "applied"})

    note_events = await database.count_recent_process_activity(
        user_id="123",
        event_type="note_create",
        since_seconds=3600,
    )
    status_events = await database.count_recent_process_activity(
        user_id="123",
        event_type="status_update",
        since_seconds=3600,
    )

    assert note_events == 2
    assert status_events == 1


@pytest.mark.asyncio
async def test_legacy_company_process_rows_migrate_into_role_scopes(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()

    company_id = await _create_company(database, company="Datadog", normalized_company="datadog")
    await database.accept_job_community_rules("123")
    await database.upsert_job_application_status(
        user_id="123",
        company_id=company_id,
        status="applied",
        notes="Initial recruiter screen happened quickly.",
    )
    await database.add_process_note(
        user_id="123",
        company_id=company_id,
        body="Second note from the legacy company-only flow.",
    )

    database._migrate_legacy_process_roles()

    rows = await database.list_process_role_statuses_for_user("123", active_only=False, limit=25)
    assert len(rows) == 1
    assert rows[0]["company"] == "Datadog"
    assert rows[0]["role_title"] == "General / Legacy"
    assert rows[0]["track"] == "legacy"
    assert rows[0]["season_term"] == "Legacy"
    assert rows[0]["note_count"] == 2
    assert rows[0]["latest_note"] == "Second note from the legacy company-only flow."

    notes = await database.list_process_role_notes(
        scope_id=int(rows[0]["scope_id"]),
        requester_user_id="123",
        include_matched=True,
        limit=25,
    )
    assert [note["body"] for note in notes] == [
        "Second note from the legacy company-only flow.",
        "Initial recruiter screen happened quickly.",
    ]
