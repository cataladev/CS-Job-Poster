from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import yaml
from dotenv import load_dotenv

from job_bot.models import AppConfig, SourceSettings
from job_bot.utils.normalization import normalize_company_name

RUNTIME_CONFIG_INTERNSHIP_CHANNEL_KEY = "discord_channel_id"
RUNTIME_CONFIG_NEW_GRAD_CHANNEL_KEY = "discord_new_grad_channel_id"
RUNTIME_CONFIG_LOG_CHANNEL_KEY = "discord_log_channel_id"
RUNTIME_CONFIG_INTERNSHIP_ALERT_ROLE_KEY = "discord_internship_alert_role_id"
RUNTIME_CONFIG_NEW_GRAD_ALERT_ROLE_KEY = "discord_new_grad_alert_role_id"
DEFAULT_COMPANY_BLACKLIST = ("dice",)


def _parse_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_int_csv(value: Optional[str], default: List[int]) -> List[int]:
    if not value:
        return default
    parsed: List[int] = []
    for item in value.split(","):
        token = item.strip()
        if not token:
            continue
        parsed.append(int(token))
    return parsed or default


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _coerce_source_settings(name: str, payload: Dict[str, Any]) -> SourceSettings:
    data = dict(payload)
    return SourceSettings(
        name=name,
        source_type=data.pop("type"),
        enabled=bool(data.pop("enabled", True)),
        priority=int(data.pop("priority", 100)),
        track=str(data.pop("track", "internship")),
        experimental=bool(data.pop("experimental", False)),
        settings=data,
    )


def load_config() -> AppConfig:
    load_dotenv(dotenv_path=Path(".env"), override=False)

    source_config_path = os.getenv("SOURCE_CONFIG_PATH", "config/sources.yaml")
    yaml_payload = _load_yaml(Path(source_config_path))
    raw_sources = yaml_payload.get("sources", {})
    source_settings = {
        name: _coerce_source_settings(name, payload)
        for name, payload in raw_sources.items()
    }

    enabled_sources = _parse_csv(os.getenv("ENABLED_SOURCES"))
    new_grad_channel_id = os.getenv("DISCORD_NEW_GRAD_CHANNEL_ID")
    internship_alert_role_id = os.getenv("DISCORD_INTERNSHIP_ALERT_ROLE_ID")
    new_grad_alert_role_id = os.getenv("DISCORD_NEW_GRAD_ALERT_ROLE_ID")
    enable_new_grad_source = _parse_bool(
        os.getenv("ENABLE_NEW_GRAD_SOURCE"),
        default=bool(new_grad_channel_id),
    )

    config = AppConfig(
        discord_token=os.getenv("DISCORD_TOKEN", ""),
        discord_channel_id=int(os.getenv("DISCORD_CHANNEL_ID", "0")),
        discord_new_grad_channel_id=int(new_grad_channel_id) if new_grad_channel_id else None,
        discord_log_channel_id=(
            int(os.getenv("DISCORD_LOG_CHANNEL_ID", "0")) or None
            if os.getenv("DISCORD_LOG_CHANNEL_ID")
            else None
        ),
        scheduled_run_hours=_parse_int_csv(os.getenv("SCHEDULED_RUN_HOURS"), [12, 20]),
        schedule_timezone=os.getenv("SCHEDULE_TIMEZONE", "America/New_York"),
        database_url=os.getenv("DATABASE_URL", ""),
        sqlite_db_path=os.getenv("SQLITE_DB_PATH", "job_bot.db"),
        remote_only=_parse_bool(os.getenv("REMOTE_ONLY")),
        us_only=_parse_bool(os.getenv("US_ONLY")),
        include_coops=_parse_bool(os.getenv("INCLUDE_COOPS"), default=True),
        season_filter=_parse_csv(os.getenv("SEASON_FILTER")),
        company_blacklist=sorted(
            {
                *(normalize_company_name(value) for value in DEFAULT_COMPANY_BLACKLIST),
                *(
                    normalize_company_name(value)
                    for value in _parse_csv(os.getenv("COMPANY_BLACKLIST"))
                ),
            }
        ),
        keyword_allowlist=[value.lower() for value in _parse_csv(os.getenv("KEYWORD_ALLOWLIST"))],
        keyword_denylist=[value.lower() for value in _parse_csv(os.getenv("KEYWORD_DENYLIST"))],
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        enabled_sources=enabled_sources,
        source_config_path=source_config_path,
        enable_new_grad_source=enable_new_grad_source,
        request_timeout_seconds=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20")),
        request_retries=int(os.getenv("REQUEST_RETRIES", "2")),
        source_concurrency=max(1, int(os.getenv("SOURCE_CONCURRENCY", "6"))),
        publish_delay_seconds=float(os.getenv("PUBLISH_DELAY_SECONDS", "1.0")),
        max_job_age_hours=int(os.getenv("MAX_JOB_AGE_HOURS", "72")),
        user_agent=os.getenv(
            "USER_AGENT",
            "cs-job-poster/2.0",
        ),
        company_seed_path=os.getenv("COMPANY_SEED_PATH", "data/company_watchlist_seed.csv"),
        source_settings=source_settings,
        poll_interval_seconds=max(30, int(os.getenv("POLL_INTERVAL_SECONDS", "300"))),
        recent_catchup_hours=max(0, int(os.getenv("RECENT_CATCHUP_HOURS", "48"))),
        digest_interval_seconds=max(900, int(os.getenv("DIGEST_INTERVAL_SECONDS", "14400"))),
        direct_ats_poll_interval_seconds=max(
            300, int(os.getenv("DIRECT_ATS_POLL_INTERVAL_SECONDS", "900"))
        ),
        discord_internship_alert_role_id=(
            int(internship_alert_role_id) if internship_alert_role_id else None
        ),
        discord_new_grad_alert_role_id=(
            int(new_grad_alert_role_id) if new_grad_alert_role_id else None
        ),
    )

    _apply_source_overrides(config)
    return config


def _apply_source_overrides(config: AppConfig) -> None:
    enabled_filter = set(config.enabled_sources)

    for name, settings in config.source_settings.items():
        if enabled_filter:
            settings.enabled = name in enabled_filter

        if settings.track == "new_grad" and (
            not config.enable_new_grad_source or config.discord_new_grad_channel_id is None
        ):
            settings.enabled = False


def apply_runtime_channel_overrides(
    config: AppConfig,
    runtime_config: Mapping[str, Mapping[str, Any]],
) -> None:
    internship_row = runtime_config.get(RUNTIME_CONFIG_INTERNSHIP_CHANNEL_KEY)
    if internship_row and internship_row.get("value"):
        config.discord_channel_id = int(internship_row["value"])

    if RUNTIME_CONFIG_NEW_GRAD_CHANNEL_KEY in runtime_config:
        raw_value = runtime_config[RUNTIME_CONFIG_NEW_GRAD_CHANNEL_KEY].get("value")
        config.discord_new_grad_channel_id = int(raw_value) if raw_value else None

    if RUNTIME_CONFIG_LOG_CHANNEL_KEY in runtime_config:
        raw_value = runtime_config[RUNTIME_CONFIG_LOG_CHANNEL_KEY].get("value")
        config.discord_log_channel_id = int(raw_value) if raw_value else None

    if RUNTIME_CONFIG_INTERNSHIP_ALERT_ROLE_KEY in runtime_config:
        raw_value = runtime_config[RUNTIME_CONFIG_INTERNSHIP_ALERT_ROLE_KEY].get("value")
        config.discord_internship_alert_role_id = int(raw_value) if raw_value else None

    if RUNTIME_CONFIG_NEW_GRAD_ALERT_ROLE_KEY in runtime_config:
        raw_value = runtime_config[RUNTIME_CONFIG_NEW_GRAD_ALERT_ROLE_KEY].get("value")
        config.discord_new_grad_alert_role_id = int(raw_value) if raw_value else None


def is_new_grad_collection_enabled(config: AppConfig) -> bool:
    return any(
        settings.track == "new_grad" and settings.enabled
        for settings in config.source_settings.values()
    )


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
