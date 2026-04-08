import pytest

from job_bot.scheduler import BotScheduler
from job_bot.source_manager import SourceManager
from job_bot.sources.base import BaseSource, SourceContext
from job_bot.models import SourceResult


class DummySource(BaseSource):
    def __init__(self, settings, phase, calls):
        super().__init__(settings)
        self.phase = phase
        self.calls = calls

    async def fetch(self, context):
        self.calls.append(self.name)
        return SourceResult(self.name, self.track, [])


@pytest.mark.asyncio
async def test_source_manager_runs_priority_before_secondary(config_factory, monkeypatch) -> None:
    config = config_factory()
    calls = []
    priority = DummySource(
        settings=type("Settings", (), {"name": "github", "track": "internship", "priority": 0, "enabled": True, "experimental": False})(),
        phase="priority",
        calls=calls,
    )
    secondary = DummySource(
        settings=type("Settings", (), {"name": "greenhouse", "track": "internship", "priority": 10, "enabled": True, "experimental": False})(),
        phase="secondary",
        calls=calls,
    )

    monkeypatch.setattr("job_bot.source_manager.build_sources", lambda _: [secondary, priority])
    context = SourceContext(config=config, database=None, http_client=None)  # type: ignore[arg-type]
    manager = SourceManager(context)

    await manager.run_priority_sources()
    await manager.run_secondary_sources()

    assert calls == ["github", "greenhouse"]


@pytest.mark.asyncio
async def test_scheduler_registers_fixed_daily_run_hours(config_factory) -> None:
    config = config_factory()

    class DummyPipeline:
        async def run(self, trigger="scheduled"):
            return None

    scheduler = BotScheduler(config, DummyPipeline())  # type: ignore[arg-type]
    scheduler.start()
    try:
        jobs = scheduler.scheduler.get_jobs()
        assert len(jobs) == 2
        hours = set()
        for job in jobs:
            hour_field = next(field for field in job.trigger.fields if field.name == "hour")
            hours.add(hour_field.expressions[0].first)
        assert hours == {12, 20}
    finally:
        scheduler.shutdown()
