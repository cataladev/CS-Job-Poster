from pathlib import Path

from job_bot.models import SourceSettings
from job_bot.sources.github_simplify import GitHubSimplifySource


def test_github_parser_handles_continuation_rows_and_terms() -> None:
    settings = SourceSettings(
        name="github_simplify_internships",
        source_type="github_simplify",
        enabled=True,
        priority=0,
        track="internship",
        settings={"repositories": []},
    )
    source = GitHubSimplifySource(settings)
    sample = Path("tests/fixtures/simplify_sample.md").read_text(encoding="utf-8")

    jobs = source._parse_markdown(
        repository="SimplifyJobs/Summer2026-Internships",
        path="README-Off-Season.md",
        markdown_text=sample,
        track="internship",
    )

    assert len(jobs) == 3
    assert jobs[0].company == "Example Corp"
    assert jobs[0].season_term == "Fall 2026"
    assert jobs[0].locations == ["NYC", "Remote in USA"]
    assert jobs[1].company == "Example Corp"
    assert jobs[1].title == "Backend Software Engineering Intern"
    assert jobs[2].status == "inactive"

