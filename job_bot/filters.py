from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from job_bot.models import AppConfig, JobPosting


ENGINEERING_TOKENS = {
    "software",
    "engineer",
    "engineering",
    "developer",
    "backend",
    "frontend",
    "fullstack",
    "platform",
    "systems",
    "ml",
    "machine learning",
    "ai",
    "firmware",
    "embedded",
    "ios",
    "android",
    "cloud",
    "devops",
    "application development",
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


@dataclass
class FilterDecision:
    accepted: bool
    reason: Optional[str] = None


class JobFilter:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def evaluate(self, job: JobPosting) -> FilterDecision:
        text = " ".join(
            [
                job.title.lower(),
                job.summary.lower(),
                " ".join(job.locations).lower(),
                (job.employment_type or "").lower(),
                (job.season_term or "").lower(),
            ]
        )

        if self._has_any_phrase(text, SENIORITY_DENY_TOKENS):
            return FilterDecision(False, "seniority_filtered")

        if not self._is_engineering_role(text):
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

        return FilterDecision(True, None)

    def _is_engineering_role(self, text: str) -> bool:
        if self._has_any_phrase(text, ROLE_DENY_TOKENS):
            return False
        return self._has_any_phrase(text, ENGINEERING_TOKENS)

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
