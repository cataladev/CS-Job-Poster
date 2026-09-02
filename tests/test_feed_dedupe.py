from __future__ import annotations

from dataclasses import asdict

import pytest

from job_bot.database import Database
from job_bot.github_feeds import JobObservation
from job_bot.utils.feed_normalization import (
    external_identity_keys,
    location_scoped_identity_keys,
    sanitize_application_url,
)
from job_bot.utils.normalization import normalize_job_posting


async def _ingest(
    database: Database,
    observation: JobObservation,
    *,
    suppress: bool = False,
    channel_id: int = 111,
):
    safe_url = sanitize_application_url(observation.application_url).url
    normalized_observation = JobObservation(
        source_id=observation.source_id,
        source_label=observation.source_label,
        source_url=observation.source_url,
        external_id=observation.external_id,
        company=observation.company,
        title=observation.title,
        locations=observation.locations,
        application_url=safe_url,
        track=observation.track,
        cycle=observation.cycle,
        posted_date=observation.posted_date,
        source_ref=observation.source_ref,
    )
    job = normalize_job_posting(normalized_observation.to_job_posting())
    exact_identity_keys = {f"url:{job.canonical_url}"}
    exact_identity_keys.update(
        external_identity_keys(
            job.canonical_url or job.url,
            company=job.normalized_company,
        )
    )
    keys = location_scoped_identity_keys(
        exact_identity_keys,
        normalized_locations=job.normalized_locations,
    )
    return await database.ingest_feed_job(
        job=job,
        source_id=normalized_observation.source_id,
        external_id=normalized_observation.external_id,
        source_label=normalized_observation.source_label,
        source_url=normalized_observation.source_url,
        source_ref=normalized_observation.source_ref,
        posted_date=normalized_observation.posted_date,
        payload=asdict(normalized_observation),
        canonical_keys=keys,
        channel_id=channel_id,
        suppress_delivery=suppress,
    )


def _observation(
    source: str,
    external_id: str,
    url: str,
    *,
    location: str = "Austin, TX",
    title: str = "Software Engineer Intern",
    posted_date: str | None = None,
) -> JobObservation:
    return JobObservation(
        source_id=source,
        source_label=source,
        source_url=f"https://github.com/{source}/jobs",
        external_id=external_id,
        company="Example Corp",
        title=title,
        locations=[location],
        application_url=url,
        track="internship",
        cycle="Summer 2027",
        posted_date=posted_date,
    )


@pytest.mark.asyncio
async def test_cross_source_same_location_is_one_delivery(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        first = await _ingest(
            database,
            _observation("source-a", "a-1", "https://careers.example.com/jobs/123?utm_source=a"),
        )
        second = await _ingest(
            database,
            _observation("source-b", "b-1", "https://apply.example.com/openings/987"),
        )

        assert first.created
        assert not second.created
        assert first.canonical_id == second.canonical_id
        assert await database.get_stats() == {
            "total_canonical_jobs": 1,
            "total_jobs_found": 2,
            "total_posted": 0,
            "total_deduplicated": 0,
        }
        assert await database.get_delivery_counts() == {"pending": 1}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_country_first_multi_city_copy_is_one_delivery(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        repository_copy = _observation(
            "repository",
            "repository-copy",
            "https://apply.careers.microsoft.com/careers/job/123",
        )
        repository_copy = JobObservation(
            **{
                **asdict(repository_copy),
                "locations": ["Redmond, WA", "Mountain View, CA"],
            }
        )
        api_copy = _observation(
            "api",
            "api-copy",
            "https://apply.careers.microsoft.com/careers/job/123",
        )
        api_copy = JobObservation(
            **{
                **asdict(api_copy),
                "locations": [
                    "United States, Washington, Redmond",
                    "United States, California, Mountain View",
                ],
            }
        )

        first = await _ingest(database, repository_copy)
        second = await _ingest(database, api_copy)

        assert first.created
        assert not second.created
        assert second.canonical_id == first.canonical_id
        assert await database.get_delivery_counts() == {"pending": 1}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_cross_source_semantic_match_requires_a_three_day_posting_window(
    config_factory,
) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        first = await _ingest(
            database,
            _observation(
                "source-a",
                "a-1",
                "https://careers.example.com/jobs/123",
                posted_date="2026-08-10",
            ),
        )
        within_window = await _ingest(
            database,
            _observation(
                "source-b",
                "b-1",
                "https://apply.example.com/openings/987",
                posted_date="2026-08-13",
            ),
        )
        outside_window = await _ingest(
            database,
            _observation(
                "source-c",
                "c-1",
                "https://jobs.example.net/roles/456",
                posted_date="2026-08-14",
            ),
        )

        assert within_window.canonical_id == first.canonical_id
        assert not within_window.created
        assert outside_window.created
        assert outside_window.canonical_id != first.canonical_id
        assert await database.get_delivery_counts() == {"pending": 2}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_exact_fingerprint_survives_shifted_semantic_date_window(
    config_factory,
) -> None:
    """An immutable fingerprint must win over mutable observation metadata."""
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        first = await _ingest(
            database,
            _observation(
                "source-a",
                "a-1",
                "https://careers.example.com/jobs/123",
                posted_date="2026-08-20",
            ),
        )
        # Reproduce the production state: a later merge has attached older
        # posting metadata, putting the canonical outside the semantic window
        # even though its unique fingerprint still carries the original date.
        database._connection.execute(  # noqa: SLF001 - regression fixture
            "UPDATE feed_observations SET posted_date = ? WHERE canonical_id = ?",
            ("2026-08-01", first.canonical_id),
        )
        database._connection.commit()  # noqa: SLF001 - regression fixture

        duplicate = await _ingest(
            database,
            _observation(
                "source-b",
                "b-1",
                "https://apply.example.com/openings/987",
                posted_date="2026-08-20",
            ),
        )

        assert not duplicate.created
        assert duplicate.canonical_id == first.canonical_id
        assert (await database.get_stats())["total_canonical_jobs"] == 1
        assert await database.get_delivery_counts() == {"pending": 1}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_stable_ats_requisition_merges_outside_semantic_posting_window(
    config_factory,
) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        direct = await _ingest(
            database,
            _observation(
                "direct-ats",
                "greenhouse-12345",
                "https://job-boards.greenhouse.io/example/jobs/12345",
                posted_date="2026-08-10",
            ),
        )
        repository_copy = await _ingest(
            database,
            _observation(
                "repo-copy",
                "greenhouse-repo-12345",
                "https://careers.example.com/openings/software?gh_jid=12345",
                posted_date="2026-08-14",
            ),
        )

        assert not repository_copy.created
        assert repository_copy.canonical_id == direct.canonical_id
        assert await database.get_delivery_counts() == {"pending": 1}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_channel_change_cannot_queue_a_second_delivery(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        first = await _ingest(
            database,
            _observation("source-a", "a-1", "https://careers.example.com/jobs/123"),
            channel_id=111,
        )
        duplicate = await _ingest(
            database,
            _observation("source-b", "b-1", "https://apply.example.com/openings/987"),
            channel_id=222,
        )

        assert first.canonical_id == duplicate.canonical_id
        assert len(await database.list_deliveries_for_send(111)) == 1
        assert await database.list_deliveries_for_send(222) == []
        assert await database.get_delivery_counts() == {"pending": 1}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_distinct_location_requisitions_remain_distinct(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        first = await _ingest(
            database,
            _observation("source-a", "a-1", "https://careers.example.com/jobs/austin"),
        )
        second = await _ingest(
            database,
            _observation(
                "source-b",
                "b-1",
                "https://careers.example.com/jobs/boston",
                location="Boston, MA",
            ),
        )

        assert first.created and second.created
        assert first.canonical_id != second.canonical_id
        assert (await database.get_stats())["total_canonical_jobs"] == 2
        assert await database.get_delivery_counts() == {"pending": 2}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_disjoint_multi_location_requisitions_remain_distinct(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        first_observation = _observation(
            "source-a",
            "multi-one",
            "https://careers.example.com/jobs/multi-one",
        )
        first_observation = JobObservation(
            **{
                **asdict(first_observation),
                "locations": ["Austin, TX", "Boston, MA"],
            }
        )
        second_observation = _observation(
            "source-b",
            "multi-two",
            "https://careers.example.com/jobs/multi-two",
        )
        second_observation = JobObservation(
            **{
                **asdict(second_observation),
                "locations": ["Seattle, WA", "New York, NY"],
            }
        )

        first = await _ingest(database, first_observation)
        second = await _ingest(database, second_observation)

        assert first.created and second.created
        assert first.canonical_id != second.canonical_id
        assert await database.get_delivery_counts() == {"pending": 2}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_distinct_titles_in_same_location_remain_distinct(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        first = await _ingest(
            database,
            _observation(
                "source-a",
                "level-one",
                "https://careers.example.com/jobs/level-one",
                title="Software Engineer Intern I",
            ),
        )
        second = await _ingest(
            database,
            _observation(
                "source-b",
                "level-two",
                "https://careers.example.com/jobs/level-two",
                title="Software Engineer Intern II",
            ),
        )

        assert first.created and second.created
        assert first.canonical_id != second.canonical_id
        assert await database.get_delivery_counts() == {"pending": 2}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_cycle_suffix_in_title_remains_distinct_without_identity_match(
    config_factory,
) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        first = await _ingest(
            database,
            _observation(
                "source-a",
                "plain-title",
                "https://careers.example.com/jobs/plain-title",
                title="Software Engineer Intern",
            ),
        )
        second = await _ingest(
            database,
            _observation(
                "source-b",
                "cycle-title",
                "https://apply.example.com/jobs/cycle-title",
                title="Software Engineer Intern - Summer 2027",
            ),
        )

        assert first.created and second.created
        assert first.canonical_id != second.canonical_id
        assert await database.get_delivery_counts() == {"pending": 2}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_near_title_and_different_location_do_not_merge_without_identity(
    config_factory,
) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        direct_ats = JobObservation(
            source_id="direct_ats_ashby_notion",
            source_label="Notion direct careers",
            source_url="https://jobs.ashbyhq.com/notion",
            external_id="notion-ats-1",
            company="Notion",
            title="Software Engineer, New Grad (Dec 2026)",
            locations=["San Francisco, CA"],
            application_url="https://jobs.ashbyhq.com/notion/notion-ats-1",
            track="new_grad",
            cycle="2026",
        )
        repository_copy = JobObservation(
            source_id="github_new_grad_list",
            source_label="GitHub new-grad list",
            source_url="https://github.com/example/new-grad",
            external_id="notion-repo-1",
            company="Notion",
            title="Software Engineer, New Grad",
            locations=["United States"],
            application_url="https://jobright.ai/jobs/info/notion-new-grad",
            track="new_grad",
            cycle="2026",
        )

        first = await _ingest(database, direct_ats)
        second = await _ingest(database, repository_copy)

        assert first.created
        assert second.created
        assert first.canonical_id != second.canonical_id
        assert await database.get_delivery_counts() == {"pending": 2}
        canonical = (await database.get_canonical_jobs([first.canonical_id]))[0]
        assert canonical.canonical_url == "https://jobs.ashbyhq.com/notion/notion-ats-1"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_coop_title_remains_distinct_from_intern_title(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        internship = await _ingest(
            database,
            _observation(
                "source-a",
                "internship",
                "https://careers.example.com/jobs/internship",
                title="Software Engineer Intern",
                posted_date="2026-08-20",
            ),
        )
        coop = await _ingest(
            database,
            _observation(
                "source-b",
                "coop",
                "https://careers.example.com/jobs/coop",
                title="Software Engineer Intern Co-op",
                posted_date="2026-08-20",
            ),
        )

        assert internship.created and coop.created
        assert internship.canonical_id != coop.canonical_id
        assert await database.get_delivery_counts() == {"pending": 2}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_overlapping_but_nonidentical_location_sets_remain_distinct(
    config_factory,
) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        multi_city = _observation(
            "source-a",
            "multi-city",
            "https://careers.example.com/jobs/multi-city",
            posted_date="2026-08-20",
        )
        multi_city = JobObservation(
            **{
                **asdict(multi_city),
                "locations": ["Austin, TX", "Boston, MA"],
            }
        )
        austin_only = _observation(
            "source-b",
            "austin-only",
            "https://careers.example.com/jobs/austin-only",
            location="Austin, TX",
            posted_date="2026-08-20",
        )

        first = await _ingest(database, multi_city)
        second = await _ingest(database, austin_only)

        assert first.created and second.created
        assert first.canonical_id != second.canonical_id
        assert await database.get_delivery_counts() == {"pending": 2}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_exact_fingerprint_match_never_attempts_duplicate_insert(
    config_factory,
) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        first = await _ingest(
            database,
            _observation(
                "source-a",
                "first",
                "https://careers.example.com/jobs/first",
                posted_date="2026-08-20",
            ),
        )
        # Simulate stale metadata that would make the broader three-day query
        # miss. The exact persisted fingerprint must still select this row.
        database._connection.execute(
            "UPDATE canonical_jobs SET first_seen = ? WHERE id = ?",
            ("2026-08-01T00:00:00+00:00", first.canonical_id),
        )
        database._connection.execute(
            "UPDATE feed_observations SET posted_date = ? WHERE canonical_id = ?",
            ("2026-08-01", first.canonical_id),
        )
        database._connection.commit()

        duplicate = await _ingest(
            database,
            _observation(
                "source-b",
                "duplicate",
                "https://careers.example.com/jobs/duplicate",
                posted_date="2026-08-20",
            ),
        )

        assert not duplicate.created
        assert duplicate.canonical_id == first.canonical_id
        assert await database.get_delivery_counts() == {"pending": 1}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_equivalent_state_names_and_specific_seasons_merge_across_sources(
    config_factory,
) -> None:
    """A broad GitHub row and Jobright's detailed copy must not double-post Notion."""
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        first = await _ingest(
            database,
            JobObservation(
                source_id="github_list",
                source_label="GitHub list",
                source_url="https://github.com/example/list",
                external_id="notion-github",
                company="🔥 Notion",
                title="Software Engineer Intern (Winter 2027)",
                locations=["San Francisco, California", "New York, New York"],
                application_url="https://www.notion.so/careers/engineering-intern",
                track="internship",
                cycle="2027",
            ),
        )
        second = await _ingest(
            database,
            JobObservation(
                source_id="jobright",
                source_label="Jobright",
                source_url="https://jobright.ai/jobs/software-engineer-intern-jobs-in-united-states",
                external_id="notion-jobright",
                company="Notion",
                title="Software Engineer Intern (Winter 2027)",
                locations=["San Francisco, CA", "New York, NY"],
                application_url="https://jobright.ai/jobs/info/notion-winter-2027",
                track="internship",
                cycle="winter 2027",
            ),
        )

        assert first.created
        assert not second.created
        assert first.canonical_id == second.canonical_id
        assert await database.get_delivery_counts() == {"pending": 1}
        canonical = (await database.get_canonical_jobs([first.canonical_id]))[0]
        assert canonical.canonical_url == "https://www.notion.so/careers/engineering-intern"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_source_edit_and_tracking_variants_do_not_requeue(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        first = await _ingest(
            database,
            _observation(
                "source-a",
                "stable-id",
                "http://careers.example.com/jobs/123?utm_source=a#apply",
            ),
        )
        edited = await _ingest(
            database,
            _observation(
                "source-a",
                "stable-id",
                "https://careers.example.com/jobs/123?ref=updated",
                title="Software Engineering Intern - Platform",
            ),
        )

        assert first.canonical_id == edited.canonical_id
        assert not edited.created
        assert await database.get_delivery_counts() == {"pending": 1}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_identity_evidence_does_not_merge_disjoint_locations(
    config_factory,
) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        austin = await _ingest(
            database,
            _observation("source-a", "a-1", "https://careers.example.com/jobs/austin"),
        )
        boston = await _ingest(
            database,
            _observation(
                "source-b",
                "b-1",
                "https://careers.example.com/jobs/boston",
                location="Boston, MA",
            ),
        )
        assert austin.canonical_id != boston.canonical_id

        bridge = await _ingest(
            database,
            _observation(
                "source-c",
                "bridge",
                "https://careers.example.com/jobs/boston",
            ),
        )

        assert not bridge.created
        assert bridge.canonical_id == austin.canonical_id
        assert bridge.merged_canonical_ids == []
        assert (await database.get_stats())["total_canonical_jobs"] == 2
        assert await database.get_delivery_counts() == {"pending": 2}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_initial_baseline_suppresses_matching_legacy_canonical(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        observation = _observation(
            "source-a",
            "legacy-copy",
            "https://careers.example.com/jobs/legacy",
        )
        legacy_job = normalize_job_posting(observation.to_job_posting())
        legacy_id, created, _ = await database.save_job(legacy_job)
        assert created

        ingested = await _ingest(database, observation, suppress=True)

        assert not ingested.created
        assert ingested.canonical_id == legacy_id
        assert ingested.delivery_state == "suppressed"
        assert await database.get_delivery_counts() == {"suppressed": 1}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_alternate_greenhouse_urls_share_one_requisition(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        board_copy = await _ingest(
            database,
            _observation(
                "source-a",
                "board-copy",
                "https://job-boards.greenhouse.io/example/jobs/12345",
            ),
        )
        careers_copy = await _ingest(
            database,
            _observation(
                "source-b",
                "careers-copy",
                "https://careers.example.com/openings/software?gh_jid=12345",
            ),
        )

        assert board_copy.canonical_id == careers_copy.canonical_id
        assert not careers_copy.created
        assert (await database.get_stats())["total_canonical_jobs"] == 1
        assert await database.get_delivery_counts() == {"pending": 1}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_same_requisition_in_disjoint_locations_remains_distinct(
    config_factory,
) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        austin = await _ingest(
            database,
            _observation(
                "source-a",
                "austin-copy",
                "https://job-boards.greenhouse.io/example/jobs/12345",
            ),
        )
        boston = await _ingest(
            database,
            _observation(
                "source-b",
                "boston-copy",
                "https://careers.example.com/openings/software?gh_jid=12345",
                location="Boston, MA",
            ),
        )

        assert austin.created and boston.created
        assert austin.canonical_id != boston.canonical_id
        assert await database.get_delivery_counts() == {"pending": 2}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_initialize_seeds_legacy_discord_posts_as_sent(config_factory) -> None:
    config = config_factory()
    first_database = Database(config.sqlite_db_path)
    await first_database.initialize()
    observation = _observation(
        "legacy-source",
        "posted",
        "https://careers.example.com/jobs/already-posted",
    )
    job = normalize_job_posting(observation.to_job_posting())
    canonical_id, _, _ = await first_database.save_job(job)
    await first_database.mark_posted(canonical_id, job.track, 111, 456)
    await first_database.close()

    reopened = Database(config.sqlite_db_path)
    await reopened.initialize()
    try:
        assert await reopened.get_delivery_counts() == {"sent": 1}
        assert await reopened.list_deliveries_for_send(111) == []
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_dateless_source_merges_with_an_older_dated_posting(config_factory) -> None:
    """A source that cannot report a posting date must not be split off by the window.

    Jobright stamps every role with its own index time, so a months-old listing looks
    like it was posted today. Carrying no date instead lets the same role seen through
    a dated repository feed still merge, rather than reaching Discord twice.
    """
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        dated = await _ingest(
            database,
            _observation(
                "source-a",
                "a-1",
                "https://careers.example.com/jobs/123",
                posted_date="2026-08-01",
            ),
        )
        dateless = await _ingest(
            database,
            _observation(
                "jobright_internships",
                "jr-1",
                "https://jobright.ai/jobs/info/abc123",
                posted_date=None,
            ),
        )

        assert dateless.canonical_id == dated.canonical_id
        assert not dateless.created
        assert await database.get_delivery_counts() == {"pending": 1}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_dateless_source_still_needs_matching_locations(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        austin = await _ingest(
            database,
            _observation(
                "source-a",
                "a-1",
                "https://careers.example.com/jobs/123",
                location="Austin, TX",
                posted_date="2026-08-01",
            ),
        )
        seattle = await _ingest(
            database,
            _observation(
                "jobright_internships",
                "jr-1",
                "https://jobright.ai/jobs/info/abc123",
                location="Seattle, WA",
                posted_date=None,
            ),
        )

        # Abstaining on the date must not weaken any other identity requirement.
        assert seattle.created
        assert seattle.canonical_id != austin.canonical_id
        assert await database.get_delivery_counts() == {"pending": 2}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_dateless_source_still_needs_matching_titles(config_factory) -> None:
    config = config_factory()
    database = Database(config.sqlite_db_path)
    await database.initialize()
    try:
        intern = await _ingest(
            database,
            _observation(
                "source-a",
                "a-1",
                "https://careers.example.com/jobs/123",
                title="Software Engineer Intern",
                posted_date="2026-08-01",
            ),
        )
        analyst = await _ingest(
            database,
            _observation(
                "jobright_internships",
                "jr-1",
                "https://jobright.ai/jobs/info/abc123",
                title="Data Analyst Intern",
                posted_date=None,
            ),
        )

        assert analyst.created
        assert analyst.canonical_id != intern.canonical_id
    finally:
        await database.close()


def test_semantic_window_abstains_only_when_the_incoming_date_is_unknown() -> None:
    from job_bot.database import _semantic_dedupe_window_matches

    # Unknown incoming date abstains rather than asserting "today".
    assert _semantic_dedupe_window_matches(
        incoming_posted_date=None,
        candidate_posted_date="2026-01-01",
        candidate_first_seen="2026-01-01T00:00:00+00:00",
    )
    # A source that does report a date is still held to the three-day window.
    assert not _semantic_dedupe_window_matches(
        incoming_posted_date="2026-08-20",
        candidate_posted_date="2026-08-01",
        candidate_first_seen="2026-08-01T00:00:00+00:00",
    )
    assert _semantic_dedupe_window_matches(
        incoming_posted_date="2026-08-03",
        candidate_posted_date="2026-08-01",
        candidate_first_seen="2026-08-01T00:00:00+00:00",
    )
    # No candidate date at all remains a non-match, dated or not.
    assert not _semantic_dedupe_window_matches(
        incoming_posted_date=None,
        candidate_posted_date=None,
        candidate_first_seen="not-a-date",
    )
