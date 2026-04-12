import pytest

from job_bot.database import Database
from job_bot.models import JobPosting, SourceResult
from job_bot.pipeline import JobPipeline


class StubPublisher:
    def __init__(self):
        self.jobs = []
        self.refreshed_jobs = []

    async def publish_jobs(self, jobs):
        self.jobs.extend(jobs)
        return [job.id for job in jobs if job.id is not None]

    async def refresh_jobs(self, jobs):
        self.refreshed_jobs.extend(jobs)
        return [job.id for job in jobs if job.id is not None]


class StubActivityLogger:
    def __init__(self):
        self.events = []

    async def __call__(self, event_name, payload):
        self.events.append((event_name, payload))


@pytest.mark.asyncio
async def test_pipeline_posts_one_canonical_job_for_duplicates(config_factory, monkeypatch) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    publisher = StubPublisher()
    activity_logger = StubActivityLogger()
    pipeline = JobPipeline(config, database, publisher, activity_logger=activity_logger)  # type: ignore[arg-type]

    class FakeSourceManager:
        def __init__(self, context):
            pass

        def list_enabled_sources(self):
            class Source:
                def __init__(self, name, phase):
                    self.name = name
                    self.phase = phase

            return [
                Source("github_simplify_internships", "priority"),
                Source("greenhouse", "secondary"),
            ]

        async def run_priority_sources(self):
            return [
                SourceResult(
                    source_name="github_simplify_internships",
                    track="internship",
                    jobs=[
                        JobPosting(
                            title="Software Engineer Intern",
                            company="Stripe",
                            locations=["NYC"],
                            source_name="github_simplify_internships",
                            track="internship",
                            url="https://linkedin.example/jobs/1",
                            summary="Summer 2026 internship",
                            raw_data={"age": "0d"},
                        )
                    ],
                )
            ]

        async def run_secondary_sources(self):
            return [
                SourceResult(
                    source_name="greenhouse",
                    track="internship",
                    jobs=[
                        JobPosting(
                            title="Software Engineering Intern",
                            company="Stripe, Inc.",
                            locations=["New York, NY"],
                            source_name="greenhouse",
                            track="internship",
                            url="https://job-boards.greenhouse.io/stripe/jobs/12345",
                            summary="Summer 2026 internship",
                            raw_data={"age": "0d"},
                        )
                    ],
                )
            ]

        async def iter_secondary_sources(self):
            for result in await self.run_secondary_sources():
                yield result

    monkeypatch.setattr("job_bot.pipeline.SourceManager", FakeSourceManager)

    stats = await pipeline.run(trigger="manual")
    persisted = await database.get_stats()

    assert stats.jobs_posted == 1
    assert len(publisher.jobs) == 1
    assert len(publisher.refreshed_jobs) == 1
    assert set(publisher.refreshed_jobs[0].source_names) == {"github_simplify_internships", "greenhouse"}
    assert persisted["total_canonical_jobs"] == 1
    assert [event for event, _ in activity_logger.events] == [
        "run_started",
        "phase_started",
        "source_finished",
        "phase_started",
        "source_finished",
        "run_finished",
    ]


def test_track_channel_routing(config_factory) -> None:
    config = config_factory()
    assert config.channel_id_for_track("internship") == 111
    assert config.channel_id_for_track("new_grad") == 222
