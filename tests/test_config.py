from pathlib import Path

from job_bot.config import load_config


def test_load_config_defaults_to_fixed_daily_schedule(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    Path("config").mkdir()
    for key in [
        "SCHEDULED_RUN_HOURS",
        "SCHEDULE_TIMEZONE",
        "CRON_INTERVAL_MINUTES",
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
