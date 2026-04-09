from __future__ import annotations

from typing import Dict, Optional

import pytest

from job_bot.models import AppConfig, SourceSettings


@pytest.fixture
def config_factory(tmp_path):
    def _factory(source_settings: Optional[Dict[str, SourceSettings]] = None) -> AppConfig:
        return AppConfig(
            discord_token="token",
            discord_channel_id=111,
            discord_new_grad_channel_id=222,
            scheduled_run_hours=[12, 20],
            schedule_timezone="America/New_York",
            database_url="",
            sqlite_db_path=str(tmp_path / "job_bot.db"),
            remote_only=False,
            us_only=False,
            include_coops=True,
            season_filter=[],
            company_blacklist=[],
            keyword_allowlist=[],
            keyword_denylist=[],
            log_level="INFO",
            enabled_sources=[],
            source_config_path="config/sources.yaml",
            enable_new_grad_source=True,
            request_timeout_seconds=5,
            request_retries=0,
            source_concurrency=2,
            publish_delay_seconds=0.0,
            max_job_age_hours=0,
            user_agent="test-agent",
            discord_admin_role_ids=[999],
            company_seed_path=str(tmp_path / "company_watchlist_seed.csv"),
            source_settings=source_settings or {},
        )

    return _factory
