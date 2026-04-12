from datetime import datetime, timezone

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
            raw_data={"age": "0d"},
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
            raw_data={"age": "0d"},
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
            posted_date=datetime.now(timezone.utc).isoformat(),
        )
    )
    decision = job_filter.evaluate(job)
    assert decision.accepted is True


def test_filter_accepts_jobs_within_three_day_window(config_factory) -> None:
    config = config_factory()
    job_filter = JobFilter(config)
    job = normalize_job_posting(
        JobPosting(
            title="Software Engineer Intern",
            company="Stripe",
            locations=["Remote in USA"],
            source_name="github",
            track="internship",
            url="https://example.com/job/4",
            summary="Backend intern role",
            raw_data={"age": "2d"},
        )
    )
    decision = job_filter.evaluate(job)
    assert decision.accepted is True


def test_filter_accepts_jobs_at_three_day_limit(config_factory) -> None:
    config = config_factory()
    job_filter = JobFilter(config)
    job = normalize_job_posting(
        JobPosting(
            title="Software Engineer Intern",
            company="Stripe",
            locations=["Remote in USA"],
            source_name="github",
            track="internship",
            url="https://example.com/job/4b",
            summary="Backend intern role",
            raw_data={"age": "3d"},
        )
    )
    decision = job_filter.evaluate(job)
    assert decision.accepted is True


def test_filter_rejects_jobs_older_than_three_day_window(config_factory) -> None:
    config = config_factory()
    job_filter = JobFilter(config)
    job = normalize_job_posting(
        JobPosting(
            title="Software Engineer Intern",
            company="Stripe",
            locations=["Remote in USA"],
            source_name="github",
            track="internship",
            url="https://example.com/job/4c",
            summary="Backend intern role",
            raw_data={"age": "4d"},
        )
    )
    decision = job_filter.evaluate(job)
    assert decision.accepted is False
    assert decision.reason == "age_filter"


def test_filter_rejects_one_day_old_jobs_when_zero_day_only(config_factory) -> None:
    config = config_factory()
    config.max_job_age_hours = 0
    job_filter = JobFilter(config)
    job = normalize_job_posting(
        JobPosting(
            title="Software Engineer Intern",
            company="Stripe",
            locations=["Remote in USA"],
            source_name="github",
            track="internship",
            url="https://example.com/job/4b",
            summary="Backend intern role",
            raw_data={"age": "1d"},
        )
    )
    decision = job_filter.evaluate(job)
    assert decision.accepted is False
    assert decision.reason == "age_filter"


def test_filter_rejects_unknown_age(config_factory) -> None:
    config = config_factory()
    job_filter = JobFilter(config)
    job = normalize_job_posting(
        JobPosting(
            title="Software Engineer Intern",
            company="Stripe",
            locations=["Remote in USA"],
            source_name="github",
            track="internship",
            url="https://example.com/job/5",
            summary="Backend intern role",
        )
    )
    decision = job_filter.evaluate(job)
    assert decision.accepted is False
    assert decision.reason == "unknown_age"


def test_filter_rejects_non_software_engineering_titles(config_factory) -> None:
    config = config_factory()
    job_filter = JobFilter(config)
    job = normalize_job_posting(
        JobPosting(
            title="Mechanical Engineering Internship - Summer 2026",
            company="Example Robotics",
            locations=["Boston, MA"],
            source_name="github",
            track="internship",
            url="https://example.com/job/6",
            summary="Listed in Engineering",
            raw_data={"age": "0d"},
        )
    )
    decision = job_filter.evaluate(job)
    assert decision.accepted is False
    assert decision.reason == "non_engineering"


def test_filter_rejects_generic_ai_intern_titles(config_factory) -> None:
    config = config_factory()
    job_filter = JobFilter(config)
    job = normalize_job_posting(
        JobPosting(
            title="AI Intern",
            company="Example AI",
            locations=["Remote in USA"],
            source_name="github",
            track="internship",
            url="https://example.com/job/7",
            summary="Build experimental models",
            raw_data={"age": "0d"},
        )
    )
    decision = job_filter.evaluate(job)
    assert decision.accepted is False
    assert decision.reason == "non_engineering"


def test_filter_accepts_curated_github_main_readme_roles_even_without_software_keywords(config_factory) -> None:
    config = config_factory()
    job_filter = JobFilter(config)
    job = normalize_job_posting(
        JobPosting(
            title="Product Manager Intern",
            company="Example",
            locations=["Remote in USA"],
            source_name="github_simplify_internships",
            track="internship",
            url="https://example.com/job/7b",
            summary="Listed in Product Management Internship Roles",
            raw_data={"age": "0d", "path": "README.md", "heading": "Product Management Internship Roles"},
        )
    )

    decision = job_filter.evaluate(job)

    assert decision.accepted is True


def test_filter_accepts_curated_github_main_readme_new_grad_roles_without_new_grad_title_signal(config_factory) -> None:
    config = config_factory()
    job_filter = JobFilter(config)
    job = normalize_job_posting(
        JobPosting(
            title="Hardware Engineer",
            company="Example",
            locations=["Austin, TX"],
            source_name="github_simplify_new_grad",
            track="new_grad",
            url="https://example.com/job/7c",
            summary="Listed in Hardware Engineering New Grad Roles",
            raw_data={"age": "1d", "path": "README.md", "heading": "Hardware Engineering New Grad Roles"},
        )
    )

    decision = job_filter.evaluate(job)

    assert decision.accepted is True


def test_filter_rejects_non_us_roles_when_us_only_enabled(config_factory) -> None:
    config = config_factory()
    config.us_only = True
    job_filter = JobFilter(config)
    job = normalize_job_posting(
        JobPosting(
            title="Software Engineer Intern",
            company="Example",
            locations=["Toronto, ON"],
            source_name="github",
            track="internship",
            url="https://example.com/job/8",
            summary="Backend intern role",
            raw_data={"age": "0d"},
        )
    )

    decision = job_filter.evaluate(job)

    assert decision.accepted is False
    assert decision.reason == "us_only"


def test_filter_accepts_us_roles_when_us_only_enabled(config_factory) -> None:
    config = config_factory()
    config.us_only = True
    job_filter = JobFilter(config)
    job = normalize_job_posting(
        JobPosting(
            title="Software Engineer Intern",
            company="Example",
            locations=["United States"],
            source_name="github",
            track="internship",
            url="https://example.com/job/9",
            summary="Backend intern role",
            raw_data={"age": "0d"},
        )
    )

    decision = job_filter.evaluate(job)

    assert decision.accepted is True
