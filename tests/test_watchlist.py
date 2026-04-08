from pathlib import Path

import pytest

from job_bot.database import Database
from job_bot.watchlist import TrackClassifier, WatchlistService


@pytest.mark.asyncio
async def test_watchlist_seed_import_creates_company_and_targets(config_factory, tmp_path) -> None:
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
async def test_company_request_approval_promotes_to_watchlist(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    service = WatchlistService(config, database)

    request_id = await service.request_company(
        company="OpenAI",
        careers_url="https://jobs.ashbyhq.com/openai",
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
