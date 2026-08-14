from datetime import datetime, timezone
from pathlib import Path

import pytest

from job_bot.bot import (
    build_companies_embed,
    build_company_info_embed,
    build_paginated_embed,
    build_recent_jobs_embed,
    sort_recent_jobs,
)
from job_bot.database import Database
from job_bot.models import CanonicalJob, JobPosting
from job_bot.utils.normalization import normalize_job_posting
from job_bot.watchlist import TrackClassifier, WatchlistService


@pytest.mark.asyncio
async def test_watchlist_seed_import_creates_company_and_targets(config_factory, tmp_path, monkeypatch) -> None:
    seed_path = tmp_path / "company_watchlist_seed.csv"
    seed_path.write_text(
        "\n".join(
            [
                "company,careers_url,homepage_url,country,categories,tier,scan_cadence,tracks,seed_source,notes,active",
                "Stripe,https://job-boards.greenhouse.io/stripe,https://stripe.com,US,big_tech, t1,every_run,both,seed_snapshot,Test,true",
            ]
        ),
        encoding="utf-8",
    )
    config = config_factory()
    config.company_seed_path = str(seed_path)
    database = Database(config.sqlite_db_path)
    await database.initialize()
    service = WatchlistService(config, database)

    async def fake_fetch(self, url, *, http_client=None):  # noqa: ANN001
        return ""

    monkeypatch.setattr("job_bot.branding.BrandingResolver._fetch_html", fake_fetch)

    stats = await service.sync_seed()
    rows = await service.list_companies()
    targets = await database.get_due_company_scan_targets(
        target_type="greenhouse",
        track="internship",
    )

    assert stats["companies_upserted"] == 1
    assert rows[0]["company"] == "Stripe"
    assert targets[0]["company"] == "Stripe"


@pytest.mark.asyncio
async def test_company_request_approval_promotes_to_watchlist(config_factory, monkeypatch) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    service = WatchlistService(config, database)

    async def fake_fetch(self, url, *, http_client=None):  # noqa: ANN001
        return ""

    monkeypatch.setattr("job_bot.branding.BrandingResolver._fetch_html", fake_fetch)

    request_id = await service.request_company(
        company="OpenAI",
        careers_url="https://jobs.ashbyhq.com/openai",
        homepage_url="https://openai.com",
        requester_user_id="123",
        requester_channel_id="456",
    )
    approved = await service.approve_request(
        request_id=request_id,
        approver_user_id="999",
    )
    targets = await database.get_company_scan_targets(int(approved["id"]))

    assert approved["company"] == "OpenAI"
    assert any(target["target_type"] == "ashby" for target in targets)
    assert approved["homepage_url"] == "https://openai.com/"


@pytest.mark.asyncio
async def test_company_request_rejects_shared_ats_homepage(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    service = WatchlistService(config, database)

    with pytest.raises(ValueError, match="Homepage URL must be a valid branded public company URL."):
        await service.request_company(
            company="OpenAI",
            careers_url="https://jobs.ashbyhq.com/openai",
            homepage_url="https://jobs.ashbyhq.com",
            requester_user_id="123",
            requester_channel_id="456",
        )


def test_track_classifier_distinguishes_internship_and_new_grad() -> None:
    classifier = TrackClassifier()

    internship = classifier.classify(
        title="Software Engineer Intern",
        summary="Backend internship",
        employment_type="Internship",
    )
    new_grad = classifier.classify(
        title="Software Engineer I",
        summary="Entry level backend role",
        employment_type="Full Time",
    )

    assert internship.track == "internship"
    assert new_grad.track == "new_grad"


def test_track_classifier_does_not_route_from_description_mentions() -> None:
    classifier = TrackClassifier()

    ambiguous = classifier.classify(
        title="Software Engineer",
        summary="Partners with intern and new grad cohorts across the company.",
        employment_type="Full Time",
    )
    explicit_new_grad = classifier.classify(
        title="Software Engineer, New Grad",
        summary="Previous internship experience is a plus.",
        employment_type="Full Time",
    )

    assert ambiguous.track is None
    assert explicit_new_grad.track == "new_grad"


@pytest.mark.asyncio
async def test_watchlist_list_companies_returns_alphabetical_order(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    service = WatchlistService(config, database)

    await database.upsert_watchlist_company(
        company="Zeta",
        normalized_company="zeta",
        careers_url="https://example.com/zeta",
        homepage_url="https://zeta.example.com",
        country="US",
        categories=["startup"],
        tier="t3",
        scan_cadence="7d",
        tracks=["both"],
        seed_source="test",
        notes="",
        active=True,
        next_scan_at=None,
    )
    await database.upsert_watchlist_company(
        company="Alpha",
        normalized_company="alpha",
        careers_url="https://example.com/alpha",
        homepage_url="https://alpha.example.com",
        country="US",
        categories=["startup"],
        tier="t3",
        scan_cadence="7d",
        tracks=["both"],
        seed_source="test",
        notes="",
        active=True,
        next_scan_at=None,
    )

    rows = await service.list_companies(limit=10)

    assert [row["company"] for row in rows] == ["Alpha", "Zeta"]


def test_build_companies_embed_shows_page_metadata() -> None:
    rows = [
        {
            "id": 1,
            "company": "Alpha",
            "tier": "t1",
            "categories": '["big_tech"]',
        },
        {
            "id": 2,
            "company": "Beta",
            "tier": "t2",
            "categories": '["fortune500"]',
        },
    ]

    embed = build_companies_embed(rows, page_index=0, query="")

    assert embed.title == "Watchlist Companies"
    assert "`#1` **Alpha**" in embed.description
    assert "Page 1/1" in embed.footer.text


def test_build_paginated_embed_uses_requested_page_size() -> None:
    embed = build_paginated_embed(
        title="Recent Jobs",
        entries=["one", "two", "three"],
        page_index=1,
        page_size=2,
    )

    assert embed.title == "Recent Jobs"
    assert embed.description == "three"
    assert "Page 2/2" in embed.footer.text


def _canonical_job(job_id: int, company: str, title: str, url: str) -> CanonicalJob:
    return CanonicalJob(
        id=job_id,
        track="internship",
        title=title,
        company=company,
        location="New York, NY",
        canonical_url=url,
        summary="Software internship",
        season_term="Summer 2026",
        employment_type=None,
        remote_status="onsite",
        status="active",
        normalized_company=company.lower(),
        normalized_title=title.lower(),
        normalized_locations=["new york ny"],
        dedupe_fingerprint=f"fingerprint-{job_id}",
        source_names=["github_simplify_internships"],
        provenance_tags=[],
        first_seen=datetime(2026, 4, 9, tzinfo=timezone.utc),
        last_seen=datetime(2026, 4, 9, tzinfo=timezone.utc),
    )


def test_build_recent_jobs_embed_uses_linked_mobile_friendly_fields() -> None:
    jobs = [
        _canonical_job(1, "Mirage", "Software Engineer - Early Career", "https://example.com/jobs/1"),
        _canonical_job(2, "OpenAI", "Software Engineer Intern", "https://example.com/jobs/2"),
    ]

    embed = build_recent_jobs_embed(jobs, page_index=0, page_size=6)

    assert embed.title == "Recent Jobs"
    assert embed.description == "Tap a role title to open the job listing."
    assert len(embed.fields) == 2
    assert embed.fields[0].name == "1. Mirage • Internship"
    assert "[Software Engineer - Early Career](https://example.com/jobs/1)" in embed.fields[0].value
    assert "New York, NY • Summer 2026 • Found 2026-04-09" in embed.fields[0].value
    assert "Page 1/1" in embed.footer.text


def test_build_recent_jobs_embed_respects_page_size() -> None:
    jobs = [
        _canonical_job(1, "Alpha", "Role 1", "https://example.com/jobs/1"),
        _canonical_job(2, "Beta", "Role 2", "https://example.com/jobs/2"),
        _canonical_job(3, "Gamma", "Role 3", "https://example.com/jobs/3"),
    ]

    embed = build_recent_jobs_embed(jobs, page_index=1, page_size=2)

    assert len(embed.fields) == 1
    assert embed.fields[0].name == "3. Gamma • Internship"
    assert "Page 2/2" in embed.footer.text


def test_sort_recent_jobs_prioritizes_fire_emoji_companies() -> None:
    regular_newer = _canonical_job(1, "Alpha", "Role 1", "https://example.com/jobs/1")
    regular_newer.first_seen = datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc)

    hot_older = _canonical_job(2, "🔥 Intel", "Role 2", "https://example.com/jobs/2")
    hot_older.first_seen = datetime(2026, 4, 9, 11, 0, tzinfo=timezone.utc)

    regular_older = _canonical_job(3, "Beta", "Role 3", "https://example.com/jobs/3")
    regular_older.first_seen = datetime(2026, 4, 9, 10, 0, tzinfo=timezone.utc)

    jobs = sort_recent_jobs([regular_newer, hot_older, regular_older])

    assert [job.company for job in jobs] == ["🔥 Intel", "Alpha", "Beta"]


def test_sort_recent_jobs_uses_hot_watchlist_display_names() -> None:
    regular_newer = _canonical_job(1, "Citadel", "Role 1", "https://example.com/jobs/1")
    regular_newer.normalized_company = "citadel"
    regular_newer.first_seen = datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc)

    regular_older = _canonical_job(2, "Beta", "Role 2", "https://example.com/jobs/2")
    regular_older.normalized_company = "beta"
    regular_older.first_seen = datetime(2026, 4, 9, 11, 0, tzinfo=timezone.utc)

    jobs = sort_recent_jobs(
        [regular_newer, regular_older],
        {"citadel": "🔥 Citadel"},
    )

    assert [job.company for job in jobs] == ["Citadel", "Beta"]

    embed = build_recent_jobs_embed(
        jobs,
        page_index=0,
        page_size=6,
        company_display_names={"citadel": "🔥 Citadel"},
    )

    assert embed.fields[0].name == "1. 🔥 Citadel • Internship"


@pytest.mark.asyncio
async def test_mark_company_hot_updates_watchlist_and_recent_jobs(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    service = WatchlistService(config, database)

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

    job = normalize_job_posting(
        JobPosting(
            title="Software Engineer Intern",
            company="Citadel",
            locations=["New York, NY"],
            source_name="github_simplify_internships",
            track="internship",
            url="https://www.citadel.com/jobs/1",
            canonical_url="https://www.citadel.com/jobs/1",
            summary="Summer 2026 internship",
            raw_data={"age": "0d"},
        )
    )
    await database.save_job(job)

    updated_company = await service.mark_company_hot("citadel")
    hot_names = await database.get_hot_watchlist_company_names()
    recent_jobs = await database.get_recent_jobs(limit=10)

    assert int(updated_company["id"]) == company_id
    assert updated_company["company"] == "🔥 Citadel"
    assert hot_names == {"citadel": "🔥 Citadel"}
    assert recent_jobs[0].company == "🔥 Citadel"


def test_build_company_info_embed_uses_mobile_friendly_sections() -> None:
    company = {
        "id": 504,
        "company": "NVIDIA",
        "tier": "t1",
        "categories": '["big_tech"]',
        "tracks": '["both"]',
        "careers_url": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
        "homepage_url": "",
        "logo_source": "workday",
        "logo_url": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/assets/logo",
        "scan_cadence": "every_run",
        "last_scan_at": "",
        "next_scan_at": "2026-04-09T00:51:29.764942+00:00",
    }
    targets = [
        {
            "target_type": "workday",
            "locator": '{"host":"nvidia.wd5.myworkdayjobs.com","site":"NVIDIAExternalCareerSite","tenant":"nvidia"}',
        }
    ]

    embed = build_company_info_embed(company, targets)

    assert embed.title == "NVIDIA"
    assert embed.url == company["careers_url"]
    assert "`#504` • Tier `t1` • Tracks `both`" in embed.description
    assert "Categories: big_tech" in embed.description
    assert embed.thumbnail.url == company["logo_url"]
    assert embed.fields[0].name == "Links"
    assert "[Open careers site](https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite)" in embed.fields[0].value
    assert "Homepage: none" in embed.fields[0].value
    assert embed.fields[1].name == "Branding"
    assert "Source: `workday`" in embed.fields[1].value
    assert "[Open logo asset](https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/assets/logo)" in embed.fields[1].value
    assert embed.fields[2].name == "Scan"
    assert "Cadence: `every_run`" in embed.fields[2].value
    assert "Last: never" in embed.fields[2].value
    assert "Next: <t:" in embed.fields[2].value
    assert embed.fields[3].name == "Targets"
    assert embed.fields[3].value == "`Workday` nvidia.wd5.myworkdayjobs.com / NVIDIAExternalCareerSite"
    assert "{" not in embed.fields[3].value
