import pytest

from job_bot.utils.feed_normalization import (
    external_identity_keys,
    has_explicit_foreign_role_location,
    is_us_location,
    location_scoped_identity_keys,
    normalize_ibm_application_url,
    preferred_application_url,
    sanitize_application_url,
)


@pytest.mark.parametrize("url", [
    "https://careers.ibm.com/careers/JobDetail?jobId=131799",
    "https://careers.ibm.com/careers/JobDetail?jobid=131799",
    "https://careers.ibm.com/en_US/careers/JobDetail?jobid=131799",
    "https://careers.ibm.com/en_US/careers/JobDetail?jobId=131799&src=jobright&jr_id=tracking",
])
def test_ibm_links_preserve_working_parameter_case_and_job_identity(url):
    expected = "https://careers.ibm.com/en_US/careers/JobDetail?jobId=131799"
    safe = sanitize_application_url(url)
    assert safe.url == expected
    assert sanitize_application_url(safe.url).url == expected
    assert external_identity_keys(url, company="ibm") == {"req:ibm:131799"}
    assert external_identity_keys(safe.url, company="ibm") == {"req:ibm:131799"}
    assert preferred_application_url(url, expected) == expected


def test_ibm_link_repair_preserves_explicit_locale():
    safe = sanitize_application_url("https://careers.ibm.com/fr_FR/careers/JobDetail?jobid=131799")
    assert safe.url == "https://careers.ibm.com/fr_FR/careers/JobDetail?jobId=131799"


@pytest.mark.parametrize("url", [
    "https://careers.other-company.com/careers/JobDetail?jobid=131799",
    "https://careers.ibm.com/en_US/careers/Login?jobid=131799",
    "https://careers.ibm.com/careers/JobDetail",
    "https://careers.ibm.com/careers/JobDetail?jobid=bad/value",
    "https://user:secret@careers.ibm.com/careers/JobDetail?jobid=131799",
])
def test_ibm_repair_only_changes_valid_ibm_job_detail_links(url):
    assert normalize_ibm_application_url(url) == url


def test_application_url_removes_tracking_and_normalizes_scheme() -> None:
    first = sanitize_application_url(
        "http://job-boards.greenhouse.io/example/jobs/123/apply?utm_source=x&gh_jid=123#apply"
    )
    second = sanitize_application_url(
        "https://job-boards.greenhouse.io/example/jobs/123?gh_jid=123&ref=board"
    )

    assert first.url == second.url
    assert first.url.startswith("https://")
    assert external_identity_keys(first.url) == {"ats:greenhouse:123"}


def test_application_url_preserves_generic_job_identity_query() -> None:
    safe = sanitize_application_url(
        "https://careers.example.com/opening?utm_source=board&id=REQ-123&session=secret"
    )

    assert safe.url == "https://careers.example.com/opening?id=REQ-123"
    assert external_identity_keys(safe.url, company="example") == {
        "req:example:req-123"
    }


def test_application_url_rejects_unsafe_destinations() -> None:
    assert sanitize_application_url("https://127.0.0.1/collect").reason == "ip-literal"
    assert sanitize_application_url("https://bit.ly/job").reason == "blocked-host"
    assert sanitize_application_url("https://example.com/job.exe").reason == "download-link"


def test_us_location_requires_evidence_unless_feed_is_trusted() -> None:
    assert is_us_location("Austin, TX", trusted_us=False)
    assert is_us_location("Virtual US", trusted_us=False)
    assert is_us_location("multiple US", trusted_us=False)
    assert is_us_location("Albuquerque, New Mexico", trusted_us=False)
    assert not is_us_location("Toronto, ON, Canada", trusted_us=True)
    assert not is_us_location("Remote in Canada", trusted_us=True)
    assert not is_us_location("Asti, Italy +1", trusted_us=True)
    assert not is_us_location("Ho Chi Minh City, Vietnam", trusted_us=True)
    assert not is_us_location("Austin, TX and Toronto, Canada", trusted_us=False)
    assert not is_us_location("Austin, TX and Milan, Italy", trusted_us=False)
    assert not is_us_location(
        "Austin, TX",
        trusted_us=False,
        context="https://jobs.example.com/germany/software-engineer",
    )
    assert is_us_location("Remote", trusted_us=True)
    assert not is_us_location("Remote", trusted_us=False)


@pytest.mark.parametrize(
    "title",
    [
        "2026 Software Dev Engineer Intern - Germany",
        "Software Engineer (India)",
        "Software Engineer, Canada - Remote",
        "Software Engineer based in United Kingdom",
    ],
)
def test_foreign_role_location_is_detected_in_title(title) -> None:
    assert has_explicit_foreign_role_location(title)


@pytest.mark.parametrize(
    "title",
    [
        "Associate Product Developer - Ninja APAC",
        "Software Engineer - New Mexico",
        "Software Engineer - Georgia",
        "Software Engineer, US Federal",
    ],
)
def test_non_location_title_text_does_not_trigger_foreign_rejection(title) -> None:
    assert not has_explicit_foreign_role_location(title)


def test_exact_identity_keys_are_scoped_to_location() -> None:
    identity = {"ats:greenhouse:123", "url:https://jobs.example.com/123"}

    austin = location_scoped_identity_keys(
        identity,
        normalized_locations=["austin tx"],
    )
    austin_reordered = location_scoped_identity_keys(
        identity,
        normalized_locations=["austin tx", "austin tx"],
    )
    boston = location_scoped_identity_keys(
        identity,
        normalized_locations=["boston ma"],
    )

    assert austin == austin_reordered
    assert austin.isdisjoint(boston)
