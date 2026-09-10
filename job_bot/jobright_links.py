"""Resolve Jobright listings to the employer's own application URL.

Jobright's public window links every role back to ``jobright.ai/jobs/info/<id>``.
The employer's real posting is served on the job's own detail route, but only to a
signed-in session: the same request returns 49 fields anonymously and 53 with a
cookie, and the four that appear are the ones that matter (``originalUrl``,
``applyLink``). Configuring ``JOBRIGHT_SESSION_ID`` turns that on; without it every
link stays exactly as it is today.

Two properties keep this cheap and safe to run continuously:

* The route is Next.js' own data endpoint, so a job costs ~12 KB of JSON rather than
  ~253 KB of HTML to parse.
* Every resolution is remembered for the life of the process. The public window holds
  the same thirty roles for hours, so a poll resolves only what it has never seen --
  a handful a day, not thirty a minute.

A dead or expired session is not an error: after a run of failed attempts the resolver
reports it once, stops trying, and every link falls back to Jobright -- which is where
they pointed before this existed.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from job_bot.utils.feed_normalization import sanitize_application_url
from job_bot.utils.http import classify_jobright_failure, extract_http_status
from job_bot.utils.notify import Notifier

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters for type checkers
    from job_bot.github_feeds import JobObservation
    from job_bot.utils.http import ImpersonatingHttpClient

JOBRIGHT_SESSION_ENV = "JOBRIGHT_SESSION_ID"
JOBRIGHT_ALERT_USER_ENV = "JOBRIGHT_ALERT_USER_ID"


def alert_mention(user_id: str) -> str:
    """Render a Discord user id as a mention, or nothing when unset."""
    user_id = (user_id or "").strip()
    return f"<@{user_id}>" if user_id.isdigit() else ""

BUILD_ID_PATTERN = re.compile(r'"buildId":"([^"]+)"')
BUILD_ID_SOURCE_URL = "https://jobright.ai/jobs/software-engineer-intern-jobs-in-united-states"

# Jobright answers a signed-in detail request in about 270ms. Four at a time keeps a
# first poll near two seconds without opening a burst of connections at one host.
RESOLVE_CONCURRENCY = 4

# Remembering every resolution forever would grow without bound in a process that runs
# for weeks. Roles leave the window long before this, so the cap only ever trims ids
# that will not be asked for again.
LINK_CACHE_LIMIT = 20_000

# Jobright occasionally serves a signed-out render to a perfectly good session, so one
# anonymous answer proves nothing. A cookie that has actually expired fails every
# request, so a run of them is the signal worth acting on. Any signed-in answer clears
# the count.
FAILURE_STREAK_LIMIT = 8

# A host Jobright refuses cannot load the deploy id either, and every job in the pass
# would otherwise re-request the same 640 KB page. One failure parks it briefly so a
# blocked deployment asks once, not thirty times a minute.
BUILD_ID_RETRY_COOLDOWN_SECONDS = 120

# Truncated so one upstream error cannot crowd out the rest of an alert.
FAILURE_DETAIL_LIMIT = 200

# What each cause means and what it asks of whoever reads the alert. Only a rejected
# cookie is fixed by replacing the cookie: naming that as the remedy for a blocked
# address or a challenged fingerprint sends someone after the wrong thing, which is
# what this used to do for every failure alike.
FAILURE_GUIDANCE: dict[str, tuple[str, str]] = {
    "session_rejected": (
        "Jobright answered a signed-out page to the configured cookie",
        "Paste a fresh JOBRIGHT_SESSION_ID and restart.",
    ),
    "interstitial": (
        "Jobright served a security check instead of its own page",
        "The address or the client fingerprint is being challenged, not the cookie. "
        "Check the Jobright egress and CHROME_IMPERSONATION.",
    ),
    "blocked": (
        "Jobright refused the job detail route",
        "Address-scoped: give the Jobright proxy pool an address it still serves.",
    ),
    "throttled": (
        "Jobright is rate-limiting the job detail route",
        "Address-scoped, and usually temporary. The links come back on their own once "
        "it lifts; a restart is what re-enables resolution.",
    ),
    "proxy_error": (
        "the Jobright proxy refused or dropped the request",
        "The egress is broken, not the session. Check the proxy list and its credentials.",
    ),
    "timeout": (
        "Jobright did not answer the job detail route in time",
        "The session is fine. Check whether this host can reach jobright.ai at all.",
    ),
    "upstream_error": (
        "Jobright answered the job detail route with a server error",
        "Nothing to fix here; it recovers when Jobright does.",
    ),
    "unreadable_payload": (
        "Jobright answered the job detail route with a payload that carried no session state",
        "The route's shape may have changed. Check jobright_link_unresolved in the log.",
    ),
}

# Said after any cause that is not the cookie, so nobody replaces a working session.
DEFAULT_REMEDY = (
    "The session itself is not the problem — check the log before replacing "
    "JOBRIGHT_SESSION_ID."
)


@dataclass(frozen=True)
class _Attempt:
    """What one resolution attempt produced, and what it proved about the failure.

    ``reason`` is empty exactly when the attempt worked. It is written in the same
    vocabulary the window's own health alerts use, so one Jobright outage reads the
    same way whether it hit the window or the links.
    """

    url: str = ""
    verdict: str = "unknown"  # authenticated | anonymous | unknown
    reason: str = ""
    detail: str = ""
    status: int | None = None


def _detail_url(build_id: str, job_id: str) -> str:
    return f"https://jobright.ai/_next/data/{build_id}/jobs/info/{job_id}.json"


def _extract_original_url(payload: object) -> tuple[str, str]:
    """Return the employer URL and what the response says about the session.

    The session verdict is deliberately three-valued. A response we could not parse
    proves nothing about whether the cookie still works, and treating it as proof
    would raise a false "session expired" alert on any transient oddity. Only a
    well-formed page that reports ``logined: false`` is evidence of a dead session.
    """
    if not isinstance(payload, Mapping):
        return "", "unknown"
    page_props = payload.get("pageProps")
    if not isinstance(page_props, Mapping) or "logined" not in page_props:
        return "", "unknown"
    session = "authenticated" if page_props.get("logined") else "anonymous"
    data_source = page_props.get("dataSource")
    job = data_source.get("jobResult") if isinstance(data_source, Mapping) else None
    if not isinstance(job, Mapping):
        return "", session
    for key in ("originalUrl", "applyLink"):
        candidate = sanitize_application_url(str(job.get(key) or "")).url
        # Jobright sometimes echoes its own wrapper here; that is not a resolution.
        if candidate and "jobright.ai" not in candidate:
            return candidate, session
    return "", session


class JobrightLinkResolver:
    """Replace Jobright wrapper links with the employer's own posting."""

    def __init__(
        self,
        session_id: str,
        *,
        notifier: Notifier | None = None,
        request_kwargs: Callable[[], dict[str, Any]] | None = None,
        concurrency: int = RESOLVE_CONCURRENCY,
        alert_mention: str = "",
    ) -> None:
        self._session_id = (session_id or "").strip()
        self._notifier = notifier
        self._alert_mention = alert_mention.strip()
        # Resolution leaves through the same egress as the window it belongs to.
        self._request_kwargs = request_kwargs or (lambda: {})
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._logger = logging.getLogger(self.__class__.__name__)
        self._build_id: str | None = None
        self._build_id_lock = asyncio.Lock()
        # job id -> employer URL, or "" for a job whose link could not be resolved.
        # Caching the empty answer matters as much as caching the hit: it stops a
        # role Jobright has no employer URL for from being retried every poll.
        self._links: dict[str, str] = {}
        self._failure_streak = 0
        # Why the failures happened, so the alert can name the real cause instead of
        # blaming the cookie for a host that cannot reach Jobright at all.
        self._failure_reasons: Counter[str] = Counter()
        self._last_failure: _Attempt | None = None
        # The deploy id is loaded once for the whole pass, so when it is what failed,
        # every job in that pass failed for the reason recorded here.
        self._build_id_failure: _Attempt | None = None
        self._build_id_retry_after = 0.0
        # Set once a session is judged dead. Resolution stops entirely rather than
        # retrying every uncached job on every poll, which would turn a dead cookie
        # into thousands of pointless authenticated requests a day.
        self._suspended = False

    @property
    def enabled(self) -> bool:
        return bool(self._session_id)

    @property
    def cached_links(self) -> int:
        return len(self._links)

    @property
    def suspended(self) -> bool:
        return self._suspended

    def _headers(self) -> dict[str, str]:
        return {"Cookie": f"SESSION_ID={self._session_id}"}

    def _egress_label(self) -> str:
        """Name the address these requests leave through, for the alert.

        Credentials never reach here: the proxy URL carries none, and curl is handed
        the pair separately.
        """
        proxy = str(self._request_kwargs().get("proxy") or "")
        return urlsplit(proxy).netloc or "direct"

    async def _ensure_build_id(self, http_client: "ImpersonatingHttpClient") -> str | None:
        """Read Next.js' deploy id, which the data route is keyed by.

        It is shared by every page and changes only when Jobright deploys, so it is
        fetched once and refreshed when the route starts answering 404.
        """
        async with self._build_id_lock:
            if self._build_id:
                return self._build_id
            if time.monotonic() < self._build_id_retry_after:
                return None
            try:
                text, _, status = await http_client.get_text(
                    BUILD_ID_SOURCE_URL,
                    headers={},
                    **self._request_kwargs(),
                )
            except (RuntimeError, TimeoutError) as exc:
                self._build_id_retry_after = (
                    time.monotonic() + BUILD_ID_RETRY_COOLDOWN_SECONDS
                )
                reason = classify_jobright_failure(exc)
                self._build_id_failure = _Attempt(
                    reason=reason,
                    detail=str(exc)[:FAILURE_DETAIL_LIMIT],
                    status=extract_http_status(exc),
                )
                self._logger.warning(
                    "jobright_build_id_unavailable reason=%s error=%s", reason, exc
                )
                return None
            match = BUILD_ID_PATTERN.search(text)
            if not match:
                self._build_id_retry_after = (
                    time.monotonic() + BUILD_ID_RETRY_COOLDOWN_SECONDS
                )
                # A page with no deploy id is not Jobright's page: an interstitial or
                # a block page renders fine and parses to nothing.
                self._build_id_failure = _Attempt(
                    reason="interstitial",
                    detail=(
                        f"the deploy id page answered {len(text)} bytes carrying no "
                        "buildId, so it was not Jobright's own page"
                    ),
                    # A challenge page is served with a perfectly ordinary status; the
                    # size and the missing buildId are what give it away.
                    status=status,
                )
                self._logger.warning(
                    "jobright_build_id_missing bytes=%s -- not a Jobright page", len(text)
                )
                return None
            self._build_id = match.group(1)
            self._build_id_failure = None
            self._logger.info("jobright_build_id_loaded build_id=%s", self._build_id)
            return self._build_id

    async def _resolve_one(
        self, http_client: "ImpersonatingHttpClient", job_id: str
    ) -> _Attempt:
        """Resolve one job, reporting why it failed when it does."""
        build_id = await self._ensure_build_id(http_client)
        if not build_id:
            # The deploy id is what failed, and it recorded its own cause.
            return self._build_id_failure or _Attempt(
                reason="interstitial",
                detail="the deploy id could not be read from Jobright's listing page",
            )
        try:
            payload, _, _ = await http_client.get_json(
                _detail_url(build_id, job_id),
                headers=self._headers(),
                **self._request_kwargs(),
            )
        except (RuntimeError, TimeoutError) as exc:
            # A stale deploy id answers 404 with an HTML error page. Drop it so the
            # next resolution reloads it, and let this job keep its Jobright link.
            if "returned 404" in str(exc):
                self._build_id = None
                self._logger.info("jobright_build_id_stale reloading_on_next_resolve")
                return _Attempt(
                    reason="stale_build_id",
                    detail="the deploy id 404s; it reloads on the next resolution",
                    status=404,
                )
            reason = classify_jobright_failure(exc)
            self._logger.warning(
                "jobright_link_unresolved job_id=%s reason=%s error=%s", job_id, reason, exc
            )
            return _Attempt(
                reason=reason,
                detail=str(exc)[:FAILURE_DETAIL_LIMIT],
                status=extract_http_status(exc),
            )

        url, verdict = _extract_original_url(payload)
        if verdict == "authenticated":
            return _Attempt(url=url, verdict="authenticated")
        if verdict == "anonymous":
            return _Attempt(
                verdict="anonymous",
                reason="session_rejected",
                detail=f"the detail route reported logined=false for job {job_id}",
                status=200,
            )
        return _Attempt(
            reason="unreadable_payload",
            detail=f"the detail route answered job {job_id} with no session state",
            status=200,
        )

    async def _record_attempt(self, attempt: _Attempt) -> None:
        """Track consecutive failures, and suspend once resolution stops working.

        Every way this fails needs a different answer, so the alert names the one that
        actually happened and quotes the error behind it. A rejected cookie is fixed
        by pasting a new one; a challenged fingerprint, a refused address, or a proxy
        that will not connect are not cookie problems at all, and telling someone to
        replace a working session sends them after the wrong thing.
        """
        if attempt.verdict == "authenticated":
            self._failure_streak = 0
            self._failure_reasons.clear()
            self._last_failure = None
            return
        self._failure_streak += 1
        self._failure_reasons[attempt.reason or "unavailable"] += 1
        self._last_failure = attempt
        if self._failure_streak < FAILURE_STREAK_LIMIT or self._suspended:
            return
        self._suspended = True

        reason, _ = self._failure_reasons.most_common(1)[0]
        breakdown = ", ".join(
            f"{name}={count}" for name, count in sorted(self._failure_reasons.items())
        )
        headline, remedy = FAILURE_GUIDANCE.get(
            reason, (f"employer links are failing ({reason})", DEFAULT_REMEDY)
        )
        egress = self._egress_label()
        status = f"HTTP {attempt.status}" if attempt.status is not None else "no response"
        self._logger.error(
            "jobright_links_suspended reason=%s status=%s egress=%s "
            "consecutive_failures=%s breakdown=%s detail=%s",
            reason,
            attempt.status if attempt.status is not None else "none",
            egress,
            self._failure_streak,
            breakdown,
            attempt.detail,
        )
        if self._notifier is None:
            return
        mention = f" {self._alert_mention}" if self._alert_mention else ""
        await self._notifier.send(
            f"🔴 Jobright employer links disabled — {headline} "
            f"({status}, egress {egress}) after {self._failure_streak} attempts. "
            f"Links have fallen back to Jobright links.\n"
            f"Reasons: {breakdown}\nLast failure: {attempt.detail}\n{remedy}{mention}"
        )

    def _remember(self, job_id: str, url: str) -> None:
        if len(self._links) >= LINK_CACHE_LIMIT:
            # Oldest first: dicts preserve insertion order, and anything this old has
            # long since left the window.
            for stale in list(self._links)[: LINK_CACHE_LIMIT // 4]:
                self._links.pop(stale, None)
        self._links[job_id] = url

    async def resolve(
        self,
        http_client: "ImpersonatingHttpClient",
        observations: Sequence["JobObservation"],
    ) -> list["JobObservation"]:
        """Point observations at the employer, keeping Jobright as the fallback.

        Anything that cannot be resolved is returned untouched, so a dead session, a
        Jobright outage, or a role with no employer URL costs the link and nothing
        else.
        """
        if not self.enabled or self._suspended or not observations:
            return list(observations)

        pending = sorted(
            {
                observation.external_id
                for observation in observations
                if observation.external_id and observation.external_id not in self._links
            }
        )

        if pending:
            async def worker(job_id: str) -> None:
                async with self._semaphore:
                    attempt = await self._resolve_one(http_client, job_id)
                await self._record_attempt(attempt)
                if attempt.verdict == "authenticated":
                    # Only a signed-in answer is worth remembering, including an empty
                    # one: it means Jobright genuinely has no employer URL for this
                    # role. A transient failure stays uncached so the next poll can
                    # try again instead of freezing the Jobright link in place.
                    self._remember(job_id, attempt.url)

            await asyncio.gather(*(worker(job_id) for job_id in pending))

        resolved: list["JobObservation"] = []
        hits = 0
        for observation in observations:
            employer_url = self._links.get(observation.external_id, "")
            if not employer_url or employer_url == observation.application_url:
                resolved.append(observation)
                continue
            hits += 1
            resolved.append(
                replace(
                    observation,
                    application_url=employer_url,
                    raw_data={
                        **observation.raw_data,
                        # The publisher renders "Apply directly" plus a "Jobright
                        # fallback" whenever this key is present, so a reader keeps a
                        # working link even if the employer's board moves the posting.
                        "jobright_fallback_url": observation.application_url,
                        "jobright_link_resolved": True,
                    },
                )
            )

        if pending:
            self._logger.info(
                "jobright_links_resolved requested=%s resolved=%s applied=%s cached=%s",
                len(pending),
                sum(1 for job_id in pending if self._links.get(job_id)),
                hits,
                len(self._links),
            )
        return resolved
