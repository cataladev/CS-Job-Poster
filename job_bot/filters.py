from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from job_bot.models import AppConfig, JobPosting


SOFTWARE_ROLE_TOKENS = {
    "software",
    "software engineer",
    "software engineering",
    "developer",
    "software developer",
    "application development",
    "application engineer",
    "web development",
    "web developer",
    "backend",
    "frontend",
    "front end",
    "fullstack",
    "full stack",
    "platform",
    "systems",
    "ml",
    "machine learning",
    "ai",
    "artificial intelligence",
    "firmware",
    "embedded",
    "ios",
    "android",
    "cloud",
    "devops",
    "site reliability",
    "sre",
    "infrastructure",
    "automation",
}
NON_SOFTWARE_DISCIPLINE_TOKENS = {
    "mechanical",
    "electrical",
    "civil",
    "chemical",
    "industrial",
    "manufacturing",
    "quality",
    "test engineer",
    "test engineering",
    "product design",
    "design operations",
    "ux",
    "ui",
    "research",
    "analog",
    "physical design",
    "verification",
    "hardware",
    "chiplet",
    "semiconductor",
    "biomedical",
}
ROLE_DENY_TOKENS = {
    "marketing",
    "finance",
    "legal",
    "human resources",
    "hr",
    "recruiter",
    "sales",
    "customer success",
    "account executive",
    "designer",
}
SENIORITY_DENY_TOKENS = {
    "senior",
    "staff",
    "principal",
    "manager",
    "director",
    "lead",
    "architect",
    "vp",
}
INTERNSHIP_TOKENS = {"intern", "internship", "coop", "co-op", "co op"}
NEW_GRAD_TOKENS = {
    "new grad",
    "new graduate",
    "entry level",
    "early career",
    "graduate",
    "software engineer i",
    "software engineer 1",
    "software developer i",
    "software developer 1",
}
US_LOCATION_TOKENS = {
    "us",
    "remote us",
    "al",
    "ak",
    "az",
    "ar",
    "ca",
    "co",
    "ct",
    "de",
    "fl",
    "ga",
    "hi",
    "ia",
    "id",
    "il",
    "in",
    "ks",
    "ky",
    "la",
    "ma",
    "md",
    "me",
    "mi",
    "mn",
    "mo",
    "ms",
    "mt",
    "nc",
    "nd",
    "ne",
    "nh",
    "nj",
    "nm",
    "nv",
    "ny",
    "oh",
    "ok",
    "or",
    "pa",
    "ri",
    "sc",
    "sd",
    "tn",
    "tx",
    "ut",
    "va",
    "vt",
    "wa",
    "wi",
    "wv",
    "wy",
}
AGE_TOKEN_RE = re.compile(r"(?P<value>\d+)\s*(?P<unit>[dhm])\b", re.IGNORECASE)


@dataclass
class FilterDecision:
    accepted: bool
    reason: Optional[str] = None


class JobFilter:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def evaluate(self, job: JobPosting) -> FilterDecision:
        title_text = job.title.lower()
        summary_text = job.summary.lower()
        employment_text = (job.employment_type or "").lower()
        location_text = " ".join(job.locations).lower()
        season_text = (job.season_term or "").lower()
        text = " ".join([title_text, summary_text, location_text, employment_text, season_text])

        if self._has_any_phrase(title_text, SENIORITY_DENY_TOKENS):
            return FilterDecision(False, "seniority_filtered")

        if not self._is_software_role(title_text, employment_text, summary_text):
            return FilterDecision(False, "non_engineering")

        if job.track == "internship":
            if not self._has_internship_signal(text):
                return FilterDecision(False, "not_internship")
            if self._has_any_phrase(text, NEW_GRAD_TOKENS):
                return FilterDecision(False, "new_grad_role")
            if not self.config.include_coops and self._has_any_phrase(text, {"coop", "co-op", "co op"}):
                return FilterDecision(False, "coops_disabled")
        elif job.track == "new_grad":
            if not self._has_new_grad_signal(text):
                return FilterDecision(False, "not_new_grad")

        if job.normalized_company in self.config.company_blacklist:
            return FilterDecision(False, "company_blacklist")

        if self.config.keyword_allowlist and not self._has_any_phrase(
            text, self.config.keyword_allowlist
        ):
            return FilterDecision(False, "keyword_allowlist")

        if self._has_any_phrase(text, self.config.keyword_denylist):
            return FilterDecision(False, "keyword_denylist")

        if self.config.remote_only and job.remote_status != "remote":
            return FilterDecision(False, "remote_only")

        if self.config.us_only and not self._is_us_role(job):
            return FilterDecision(False, "us_only")

        if self.config.season_filter:
            season = (job.season_term or "").lower()
            if not season or season not in self.config.season_filter:
                return FilterDecision(False, "season_filter")

        age_hours = self._job_age_hours(job)
        if age_hours is None:
            return FilterDecision(False, "unknown_age")
        if age_hours > self.config.max_job_age_hours:
            return FilterDecision(False, "age_filter")

        return FilterDecision(True, None)

    def _is_software_role(
        self,
        title_text: str,
        employment_text: str,
        summary_text: str,
    ) -> bool:
        title_context = " ".join([title_text, employment_text]).strip()
        combined_text = " ".join([title_context, summary_text]).strip()

        if self._has_any_phrase(title_context, ROLE_DENY_TOKENS):
            return False
        if self._has_any_phrase(title_context, NON_SOFTWARE_DISCIPLINE_TOKENS):
            return False
        if self._has_any_phrase(title_context, SOFTWARE_ROLE_TOKENS):
            return True

        # Let summaries rescue generic titles like "Intern" only when the summary
        # contains a clear software-role signal rather than a generic engineering header.
        if self._has_any_phrase(summary_text, SOFTWARE_ROLE_TOKENS) and (
            self._has_any_phrase(title_text, INTERNSHIP_TOKENS)
            or self._has_any_phrase(title_text, NEW_GRAD_TOKENS)
        ):
            return True

        return self._has_any_phrase(combined_text, {"software engineer", "software developer"})

    def _has_internship_signal(self, text: str) -> bool:
        return self._has_any_phrase(text, INTERNSHIP_TOKENS)

    def _has_new_grad_signal(self, text: str) -> bool:
        return self._has_any_phrase(text, NEW_GRAD_TOKENS)

    def _has_any_phrase(self, text: str, phrases) -> bool:
        for phrase in phrases:
            pattern = r"\b" + re.escape(phrase) + r"\b"
            if re.search(pattern, text):
                return True
        return False

    def _is_us_role(self, job: JobPosting) -> bool:
        if not job.normalized_locations:
            return False
        for location in job.normalized_locations:
            if "remote us" in location or " usa" in location or location == "us":
                return True
            tokens = set(location.split())
            if tokens.intersection(US_LOCATION_TOKENS):
                return True
        return False

    def _job_age_hours(self, job: JobPosting) -> Optional[float]:
        raw_age = str(job.raw_data.get("age", "")).strip().lower()
        if raw_age:
            parsed_age = self._parse_relative_age(raw_age)
            if parsed_age is not None:
                return parsed_age
        if job.posted_date:
            return self._parse_posted_date(job.posted_date)
        return None

    def _parse_relative_age(self, value: str) -> Optional[float]:
        if value in {"today", "new", "just now"}:
            return 0.0
        if value == "yesterday":
            return 24.0

        match = AGE_TOKEN_RE.search(value)
        if not match:
            return None

        amount = int(match.group("value"))
        unit = match.group("unit").lower()
        if unit == "d":
            return float(amount * 24)
        if unit == "h":
            return float(amount)
        if unit == "m":
            return amount / 60.0
        return None

    def _parse_posted_date(self, value: str) -> Optional[float]:
        raw = str(value).strip()
        if not raw:
            return None

        parsed: Optional[datetime] = None
        if raw.isdigit():
            timestamp = int(raw)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        else:
            normalized = raw.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                try:
                    parsed = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except ValueError:
                    return None

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        age = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
        return max(age.total_seconds() / 3600, 0.0)
