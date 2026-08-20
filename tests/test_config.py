from pathlib import Path

from job_bot.config import load_config


def test_load_config_defaults_to_five_minute_poll(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    Path("config").mkdir()
    for key in [
        "SCHEDULED_RUN_HOURS",
        "SCHEDULE_TIMEZONE",
        "DISCORD_TOKEN",
        "DISCORD_CHANNEL_ID",
        "DISCORD_NEW_GRAD_CHANNEL_ID",
        "ENABLE_NEW_GRAD_SOURCE",
        "DATABASE_URL",
        "SQLITE_DB_PATH",
        "SOURCE_CONFIG_PATH",
        "POLL_INTERVAL_SECONDS",
        "DIGEST_INTERVAL_SECONDS",
        "DISCORD_INTERNSHIP_ALERT_ROLE_ID",
        "DISCORD_NEW_GRAD_ALERT_ROLE_ID",
        "DIRECT_ATS_POLL_INTERVAL_SECONDS",
        "COMPANY_SOURCE_POLL_INTERVAL_SECONDS",
    ]:
        monkeypatch.delenv(key, raising=False)

    config = load_config()

    assert config.scheduled_run_hours == [12, 20]
    assert config.schedule_timezone == "America/New_York"
    assert config.database_url == ""
    assert config.database_dsn == "job_bot.db"
    assert config.max_job_age_hours == 72
    assert config.poll_interval_seconds == 300
    assert config.digest_interval_seconds == 14_400
    assert config.digest_hour_local == 16
    assert config.digest_timezone == "America/New_York"
    assert config.discord_internship_alert_role_id is None
    assert config.discord_new_grad_alert_role_id is None
    assert config.direct_ats_poll_interval_seconds == 900
    assert config.company_source_poll_interval_seconds == 3600
    assert config.company_blacklist == ["dice"]


def test_poll_interval_has_safe_minimum(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    Path("config").mkdir()
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "5")

    config = load_config()

    assert config.poll_interval_seconds == 30


def test_digest_settings_support_role_pings_and_a_safe_minimum(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    Path("config").mkdir()
    monkeypatch.setenv("DIGEST_INTERVAL_SECONDS", "5")
    monkeypatch.setenv("DISCORD_INTERNSHIP_ALERT_ROLE_ID", "555")
    monkeypatch.setenv("DISCORD_NEW_GRAD_ALERT_ROLE_ID", "666")

    config = load_config()

    assert config.digest_interval_seconds == 900
    assert config.discord_internship_alert_role_id == 555
    assert config.discord_new_grad_alert_role_id == 666


def test_blank_digest_role_environment_values_disable_role_pings(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    Path("config").mkdir()
    monkeypatch.setenv("DISCORD_INTERNSHIP_ALERT_ROLE_ID", "")
    monkeypatch.setenv("DISCORD_NEW_GRAD_ALERT_ROLE_ID", "")

    config = load_config()

    assert config.discord_internship_alert_role_id is None
    assert config.discord_new_grad_alert_role_id is None


def test_direct_ats_poll_interval_has_safe_minimum(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    Path("config").mkdir()
    monkeypatch.setenv("DIRECT_ATS_POLL_INTERVAL_SECONDS", "5")

    assert load_config().direct_ats_poll_interval_seconds == 300


def test_company_source_poll_interval_has_safe_minimum(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    Path("config").mkdir()
    monkeypatch.setenv("COMPANY_SOURCE_POLL_INTERVAL_SECONDS", "5")

    assert load_config().company_source_poll_interval_seconds == 300


def test_load_config_prefers_database_url_over_sqlite_path(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    Path("config").mkdir()
    monkeypatch.setenv("DATABASE_URL", "postgresql://jobs:secret@localhost:5432/jobbot")
    monkeypatch.setenv("SQLITE_DB_PATH", "ignored.db")

    config = load_config()

    assert config.database_url == "postgresql://jobs:secret@localhost:5432/jobbot"
    assert config.database_dsn == "postgresql://jobs:secret@localhost:5432/jobbot"
