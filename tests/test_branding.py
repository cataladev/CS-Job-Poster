from pathlib import Path

import pytest

from job_bot.branding import BrandingResolver, sanitize_brand_homepage_url
from job_bot.database import Database
from job_bot.watchlist import WatchlistService


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "branding"


def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_branding_extractor_handles_supported_providers(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    resolver = BrandingResolver(config, database)

    assert (
        resolver._extract_provider_logo(
            "ashby",
            _fixture("ashby.html"),
            "https://jobs.ashbyhq.com/openai/123",
        )
        == "https://app.ashbyhq.com/api/images/org-theme-logo/org/theme/logo.png"
    )
    assert (
        resolver._extract_provider_logo(
            "lever",
            _fixture("lever.html"),
            "https://jobs.lever.co/example/123",
        )
        == "https://lever-client-logos.s3.us-west-2.amazonaws.com/example-logo.png"
    )
    assert (
        resolver._extract_provider_logo(
            "workday",
            _fixture("workday.html"),
            "https://intel.wd1.myworkdayjobs.com/en-us/external/job/123",
        )
        == "https://intel.wd1.myworkdayjobs.com/en-us/external/assets/logo"
    )


@pytest.mark.asyncio
async def test_greenhouse_banner_only_does_not_count_as_logo(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    resolver = BrandingResolver(config, database)

    logo_url = resolver._extract_provider_logo(
        "greenhouse",
        _fixture("greenhouse_banner_only.html"),
        "https://job-boards.greenhouse.io/example/jobs/123",
    )

    assert logo_url is None


@pytest.mark.asyncio
async def test_sync_seed_clears_ats_homepage_and_caches_verified_logo(
    config_factory,
    monkeypatch,
    tmp_path,
) -> None:
    seed_path = tmp_path / "company_watchlist_seed.csv"
    seed_path.write_text(
        "\n".join(
            [
                "company,careers_url,homepage_url,country,categories,tier,scan_cadence,tracks,seed_source,notes,active",
                "OpenAI,https://jobs.ashbyhq.com/openai,https://jobs.ashbyhq.com,US,big_tech,t1,every_run,both,seed_snapshot,Test,true",
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
        return _fixture("ashby.html")

    monkeypatch.setattr("job_bot.branding.BrandingResolver._fetch_html", fake_fetch)

    stats = await service.sync_seed(force_brand_refresh=True)
    row = await database.find_watchlist_company("OpenAI")

    assert stats["logos_verified"] == 1
    assert row["homepage_url"] == ""
    assert row["logo_source"] == "ashby"
    assert row["logo_url"] == "https://app.ashbyhq.com/api/images/org-theme-logo/org/theme/logo.png"
    assert row["logo_verified_at"] is not None


@pytest.mark.asyncio
async def test_sync_seed_preserves_branded_homepage_and_caches_favicon(config_factory, tmp_path) -> None:
    seed_path = tmp_path / "company_watchlist_seed.csv"
    seed_path.write_text(
        "\n".join(
            [
                "company,careers_url,homepage_url,country,categories,tier,scan_cadence,tracks,seed_source,notes,active",
                "Stripe,https://stripe.com/jobs/search,https://stripe.com,US,big_tech,t1,every_run,both,seed_snapshot,Test,true",
            ]
        ),
        encoding="utf-8",
    )
    config = config_factory()
    config.company_seed_path = str(seed_path)
    database = Database(config.sqlite_db_path)
    await database.initialize()
    service = WatchlistService(config, database)

    stats = await service.sync_seed(force_brand_refresh=False)
    row = await database.find_watchlist_company("Stripe")

    assert stats["logos_verified"] == 1
    assert row["homepage_url"] == "https://stripe.com/"
    assert row["logo_source"] == "homepage_favicon"
    assert row["logo_url"] == "https://icons.duckduckgo.com/ip3/stripe.com.ico"


def test_sanitize_brand_homepage_url_rejects_shared_ats_hosts() -> None:
    assert sanitize_brand_homepage_url("https://jobs.lever.co/example") == ""
    assert sanitize_brand_homepage_url("https://job-boards.greenhouse.io/example") == ""
    assert sanitize_brand_homepage_url("https://stripe.com/jobs") == "https://stripe.com/jobs"
