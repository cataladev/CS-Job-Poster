from pathlib import Path

from job_bot.config import load_config


def test_load_config_defaults_to_fixed_daily_schedule(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    Path("config").mkdir()
    for key in [
        "SCHEDULED_RUN_HOURS",
        "SCHEDULE_TIMEZONE",
        "DISCORD_TOKEN",
        "DISCORD_CHANNEL_ID",
        "DISCORD_NEW_GRAD_CHANNEL_ID",
        "ENABLE_NEW_GRAD_SOURCE",
        "SOURCE_CONFIG_PATH",
    ]:
        monkeypatch.delenv(key, raising=False)

    config = load_config()

    assert config.scheduled_run_hours == [12, 20]
    assert config.schedule_timezone == "America/New_York"
    assert config.database_url == ""
    assert config.database_dsn == "job_bot.db"


def test_load_config_prefers_database_url_over_sqlite_path(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    Path("config").mkdir()
    monkeypatch.setenv("DATABASE_URL", "postgresql://jobs:secret@localhost:5432/jobbot")
    monkeypatch.setenv("SQLITE_DB_PATH", "ignored.db")

    config = load_config()

    assert config.database_url == "postgresql://jobs:secret@localhost:5432/jobbot"
    assert config.database_dsn == "postgresql://jobs:secret@localhost:5432/jobbot"
