from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

from job_bot.database import Database, DbRow
from job_bot.models import AppConfig, CanonicalJob, JobPosting
from job_bot.utils.http import AsyncHttpClient
from job_bot.utils.normalization import canonicalize_url


# These are multi-tenant ATS or recruiting hosts. They are valid careers URLs, but they
# are not trustworthy "company homepage" domains for favicon-based branding.
SHARED_BRANDING_HOST_SUFFIXES = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "myworkdaysite.com",
    "smartrecruiters.com",
    "oraclecloud.com",
    "icims.com",
    "applytojob.com",
    "workatastartup.com",
    "simplify.jobs",
    "jobs2web.com",
    "workable.com",
    "bamboohr.com",
    "eightfold.ai",
    "jobvite.com",
    "gem.com",
    "rippling.com",
)
# TODO: Extend strict provider parsing to other ATSes only when they expose a stable,
# provider-specific logo signal. Do not add domain guessing or search-based fallback here.
SUPPORTED_PROVIDER_SOURCES = {"ashby", "lever", "workday", "greenhouse"}

ASHBY_LOGO_PATTERNS = (
    r'"logo"\s*:\s*"(?P<url>https://app\.ashbyhq\.com/api/images/org-theme-logo/[^"]+)"',
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](?P<url>https://app\.ashbyhq\.com/api/images/[^"\']+)"',
    r'(?P<url>https://app\.ashbyhq\.com/api/images/org-theme-logo/[^"\'>\s]+)',
)
LEVER_LOGO_PATTERNS = (
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](?P<url>https://lever-client-logos[^"\']+)"',
    r'(?P<url>https://lever-client-logos[^"\'>\s]+)',
)
WORKDAY_LOGO_PATTERNS = (
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](?P<url>[^"\']+/assets/logo[^"\']*)',
    r'(?P<url>https?://[^"\'>\s]+/assets/logo[^"\'>\s]*)',
)
GREENHOUSE_LOGO_PATTERNS = (
    r'(?P<url>https://[^"\'>\s]*(?:logo|wordmark)[^"\'>\s]*)',
    r'"(?:logo|wordmark)(?:Url)?"\s*:\s*"(?P<url>https://[^"]+)"',
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_url_host(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return parsed.netloc.lower()


def is_shared_branding_host(host_or_url: str) -> bool:
    host = host_or_url.lower()
    if "://" in host_or_url:
        host = extract_url_host(host_or_url)
    if not host:
        return False
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in SHARED_BRANDING_HOST_SUFFIXES)


def is_strict_brand_homepage_url(url: str) -> bool:
    host = extract_url_host(url)
    if not host:
        return False
    return not is_shared_branding_host(host)


def sanitize_brand_homepage_url(url: str) -> str:
    canonical = canonicalize_url(url.strip()) if url else ""
    if not canonical:
        return ""
    return canonical if is_strict_brand_homepage_url(canonical) else ""


def build_favicon_url(url: str) -> Optional[str]:
    host = extract_url_host(url)
    if not host or is_shared_branding_host(host):
        return None
    return f"https://icons.duckduckgo.com/ip3/{host}.ico"


@dataclass
class BrandingResolution:
    homepage_url: str
    logo_url: str
    logo_source: str
    logo_verified_at: Optional[str]
    changed: bool


class BrandingResolver:
    def __init__(self, config: AppConfig, database: Database) -> None:
        self.config = config
        self.database = database
        self.logger = logging.getLogger(self.__class__.__name__)

    async def ensure_company_branding(
        self,
        *,
        company_row: DbRow,
        job_url: str = "",
        force: bool = False,
        http_client: Optional[AsyncHttpClient] = None,
    ) -> BrandingResolution:
        # This method is the single place that decides whether a company gets a "real"
        # logo, a branded favicon, or the Discord-side troll fallback. Keep it strict:
        # verified provider asset first, then verified branded domain only.
        company_id = int(company_row["id"])
        current_homepage = sanitize_brand_homepage_url(company_row["homepage_url"] or "")
        current_logo_url = (company_row["logo_url"] or "").strip()
        current_logo_source = (company_row["logo_source"] or "").strip()
        current_verified_at = company_row["logo_verified_at"]
        homepage_changed = current_homepage != (company_row["homepage_url"] or "")

        if current_logo_source == "manual" and not force:
            changed = await self.database.update_watchlist_branding(
                company_id=company_id,
                homepage_url=current_homepage,
            )
            return BrandingResolution(
                homepage_url=current_homepage,
                logo_url=current_logo_url,
                logo_source=current_logo_source,
                logo_verified_at=current_verified_at,
                changed=changed,
            )

        if not force and current_verified_at and current_logo_source:
            changed = False
            if homepage_changed:
                changed = await self.database.update_watchlist_branding(
                    company_id=company_id,
                    homepage_url=current_homepage,
                )
            return BrandingResolution(
                homepage_url=current_homepage,
                logo_url=current_logo_url,
                logo_source=current_logo_source,
                logo_verified_at=current_verified_at,
                changed=changed,
            )

        provider_url = self._choose_provider_url(job_url=job_url, careers_url=company_row["careers_url"] or "")
        logo_url = ""
        logo_source = ""

        if provider_url:
            provider = self._provider_for_url(provider_url)
            if provider:
                logo_url = await self._resolve_provider_logo(provider, provider_url, http_client=http_client) or ""
                if logo_url:
                    logo_source = provider

        if not logo_url:
            favicon_url = build_favicon_url(current_homepage)
            if favicon_url:
                logo_url = favicon_url
                logo_source = "homepage_favicon"
            else:
                careers_favicon = build_favicon_url(company_row["careers_url"] or "")
                if careers_favicon:
                    logo_url = careers_favicon
                    logo_source = "careers_favicon"
                else:
                    logo_source = "none"

        verified_at = _utc_now()
        changed = await self.database.update_watchlist_branding(
            company_id=company_id,
            homepage_url=current_homepage,
            logo_url=logo_url,
            logo_source=logo_source,
            logo_verified_at=verified_at,
        )
        return BrandingResolution(
            homepage_url=current_homepage,
            logo_url=logo_url,
            logo_source=logo_source,
            logo_verified_at=verified_at,
            changed=changed,
        )

    async def ensure_branding_for_posting(
        self,
        job: JobPosting,
        *,
        http_client: Optional[AsyncHttpClient] = None,
    ) -> Optional[BrandingResolution]:
        if not job.normalized_company:
            return None
        company_row = await self.database.get_watchlist_company_by_normalized_company(job.normalized_company)
        if not company_row:
            return None
        return await self.ensure_company_branding(
            company_row=company_row,
            job_url=job.canonical_url or job.url,
            force=False,
            http_client=http_client,
        )

    async def resolve_logo_url_for_job(
        self,
        job: CanonicalJob,
        *,
        http_client: Optional[AsyncHttpClient] = None,
    ) -> Optional[str]:
        # Canonical jobs should normally resolve through cached company branding. The
        # provider fallback below exists for jobs that were seen before the watchlist row
        # was enriched or for one-off canonical URLs outside the watchlist.
        if job.normalized_company:
            company_row = await self.database.get_watchlist_company_by_normalized_company(job.normalized_company)
            if company_row:
                resolution = await self.ensure_company_branding(
                    company_row=company_row,
                    job_url=job.canonical_url,
                    force=False,
                    http_client=http_client,
                )
                if resolution.logo_url:
                    return resolution.logo_url

        provider_url = self._choose_provider_url(job_url=job.canonical_url, careers_url="")
        if provider_url:
            provider = self._provider_for_url(provider_url)
            if provider:
                logo_url = await self._resolve_provider_logo(provider, provider_url, http_client=http_client)
                if logo_url:
                    return logo_url

        return build_favicon_url(job.canonical_url)

    def _choose_provider_url(self, *, job_url: str, careers_url: str) -> str:
        candidate = canonicalize_url(job_url) if job_url else ""
        if self._provider_for_url(candidate):
            return candidate
        candidate = canonicalize_url(careers_url) if careers_url else ""
        if self._provider_for_url(candidate):
            return candidate
        return ""

    def _provider_for_url(self, url: str) -> Optional[str]:
        host = extract_url_host(url)
        if not host:
            return None
        if host.endswith("ashbyhq.com"):
            return "ashby"
        if host.endswith("lever.co"):
            return "lever"
        if "myworkdayjobs.com" in host or "myworkdaysite.com" in host:
            return "workday"
        if host.endswith("greenhouse.io"):
            return "greenhouse"
        return None

    async def _resolve_provider_logo(
        self,
        provider: str,
        url: str,
        *,
        http_client: Optional[AsyncHttpClient] = None,
    ) -> Optional[str]:
        html = await self._fetch_html(url, http_client=http_client)
        if not html:
            return None
        return self._extract_provider_logo(provider, html, url)

    async def _fetch_html(
        self,
        url: str,
        *,
        http_client: Optional[AsyncHttpClient] = None,
    ) -> str:
        try:
            if http_client is not None:
                text, _, _ = await http_client.get_text(url, allow_redirects=True)
                return text
            async with AsyncHttpClient(
                timeout_seconds=self.config.request_timeout_seconds,
                retries=self.config.request_retries,
                user_agent=self.config.user_agent,
            ) as client:
                text, _, _ = await client.get_text(url, allow_redirects=True)
                return text
        except Exception as exc:
            self.logger.debug("Branding fetch failed for %s: %s", url, exc)
            return ""

    def _extract_provider_logo(self, provider: str, html: str, base_url: str) -> Optional[str]:
        patterns = {
            "ashby": ASHBY_LOGO_PATTERNS,
            "lever": LEVER_LOGO_PATTERNS,
            "workday": WORKDAY_LOGO_PATTERNS,
            "greenhouse": GREENHOUSE_LOGO_PATTERNS,
        }.get(provider, ())

        # Each extractor intentionally uses a tiny, provider-specific ruleset instead of
        # generic "first image on the page" parsing. False positives here are worse than
        # missing a logo because they can silently misbrand posts in Discord.
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if not match:
                continue
            candidate = match.groupdict().get("url") or match.group(0)
            normalized = urljoin(base_url, candidate)
            if self._is_valid_provider_logo(provider, normalized):
                return normalized
        return None

    def _is_valid_provider_logo(self, provider: str, url: str) -> bool:
        lowered = url.lower()
        if provider == "ashby":
            return "org-theme-logo" in lowered or "/logo" in lowered
        if provider == "lever":
            return "lever-client-logos" in lowered
        if provider == "workday":
            return "/assets/logo" in lowered
        if provider == "greenhouse":
            return ("logo" in lowered or "wordmark" in lowered) and "banner" not in lowered
        return False
