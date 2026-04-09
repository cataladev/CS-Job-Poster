from pathlib import Path

import pytest

from job_bot.bot import build_companies_embed, build_paginated_embed
from job_bot.database import Database
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
