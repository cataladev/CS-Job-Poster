from job_bot.utils.feed_normalization import (
    external_identity_keys,
    is_us_location,
    location_scoped_identity_keys,
    sanitize_application_url,
)


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
    assert not is_us_location("Toronto, ON, Canada", trusted_us=True)
    assert not is_us_location("Remote in Canada", trusted_us=True)
    assert not is_us_location("Asti, Italy +1", trusted_us=True)
    assert not is_us_location("Ho Chi Minh City, Vietnam", trusted_us=True)
    assert not is_us_location("Austin, TX and Toronto, Canada", trusted_us=False)
    assert is_us_location("Remote", trusted_us=True)
    assert not is_us_location("Remote", trusted_us=False)


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
