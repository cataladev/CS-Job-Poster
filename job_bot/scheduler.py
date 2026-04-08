from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from job_bot.models import AppConfig
from job_bot.pipeline import JobPipeline


class BotScheduler:
    def __init__(self, config: AppConfig, pipeline: JobPipeline) -> None:
        self.config = config
        self.pipeline = pipeline
        self.logger = logging.getLogger(self.__class__.__name__)
        self.scheduler = AsyncIOScheduler(timezone=ZoneInfo(config.schedule_timezone))
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        for hour in sorted(set(self.config.scheduled_run_hours)):
            self.scheduler.add_job(
                self._scheduled_run,
                trigger=CronTrigger(
                    hour=hour,
                    minute=0,
                    timezone=ZoneInfo(self.config.schedule_timezone),
                ),
                id=f"job-discovery-run-{hour:02d}00",
                max_instances=1,
                coalesce=True,
            )
        self.scheduler.start()
        self.logger.info(
            "Scheduler started for daily runs at %s:00 (%s)",
            ", ".join(f"{hour:02d}" for hour in sorted(set(self.config.scheduled_run_hours))),
            self.config.schedule_timezone,
        )
        self._started = True

    def shutdown(self) -> None:
        if self._started:
            self.scheduler.shutdown(wait=False)
            self._started = False

    async def _scheduled_run(self) -> None:
        stats = await self.pipeline.run(trigger="scheduled")
        self.logger.info(
            "Scheduled run complete: fetched=%s filtered=%s duplicates=%s posted=%s errors=%s",
            stats.jobs_fetched,
            stats.jobs_filtered,
            stats.duplicates_removed,
            stats.jobs_posted,
            len(stats.errors),
        )
