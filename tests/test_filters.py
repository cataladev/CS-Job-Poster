from job_bot.filters import JobFilter
from job_bot.models import JobPosting
from job_bot.utils.normalization import normalize_job_posting


def test_filter_accepts_relevant_internship(config_factory) -> None:
    config = config_factory()
    job_filter = JobFilter(config)
    job = normalize_job_posting(
        JobPosting(
            title="Software Engineer Intern",
            company="Stripe",
            locations=["Remote in USA"],
            source_name="github",
            track="internship",
            url="https://example.com/job/1",
            summary="Backend intern role",
        )
    )
    decision = job_filter.evaluate(job)
    assert decision.accepted is True


def test_filter_rejects_non_engineering_roles(config_factory) -> None:
    config = config_factory()
    job_filter = JobFilter(config)
    job = normalize_job_posting(
        JobPosting(
            title="Marketing Intern",
            company="Stripe",
            locations=["New York, NY"],
            source_name="github",
            track="internship",
            url="https://example.com/job/2",
            summary="Marketing internship",
        )
    )
    decision = job_filter.evaluate(job)
    assert decision.accepted is False
    assert decision.reason == "non_engineering"


def test_filter_accepts_new_grad_track(config_factory) -> None:
    config = config_factory()
    job_filter = JobFilter(config)
    job = normalize_job_posting(
        JobPosting(
            title="Software Engineer I",
            company="Visa",
            locations=["Austin, TX"],
            source_name="github_new_grad",
            track="new_grad",
            url="https://example.com/job/3",
            summary="New grad software engineer",
        )
    )
    decision = job_filter.evaluate(job)
    assert decision.accepted is True

