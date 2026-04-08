from __future__ import annotations

from typing import Iterable, List, Optional
from urllib.parse import urlparse

from rapidfuzz import fuzz

from job_bot.database import Database
from job_bot.models import CanonicalJob, DedupeDecision, JobPosting

SPECIALIZATION_TOKENS = {
    "backend",
    "frontend",
    "fullstack",
    "ios",
    "android",
    "mobile",
    "embedded",
    "firmware",
    "platform",
    "ml",
    "ai",
    "data",
    "security",
}


class DedupeEngine:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def resolve(self, job: JobPosting) -> DedupeDecision:
        exact_keys = self._exact_keys(job)
        exact = await self.database.get_canonical_by_fingerprint_keys(exact_keys)
        if exact:
            return DedupeDecision(
                is_duplicate=True,
                canonical_id=exact.id,
                reason="exact_identifier",
                score=100.0,
                matched_job=exact,
            )

        strong = await self.database.get_canonical_by_fingerprint_keys(
            [f"strong:{job.dedupe_fingerprint}"]
        )
        if strong:
            return DedupeDecision(
                is_duplicate=True,
                canonical_id=strong.id,
                reason="strong_fingerprint",
                score=99.0,
                matched_job=strong,
            )

        candidates = await self.database.get_fuzzy_candidates(
            track=job.track,
            normalized_company=job.normalized_company,
            season_term=job.season_term,
        )
        match, score = self._best_fuzzy_match(job, candidates)
        if match and score is not None:
            if score >= 92:
                return DedupeDecision(
                    is_duplicate=True,
                    canonical_id=match.id,
                    reason="fuzzy_auto_merge",
                    score=score,
                    matched_job=match,
                )
            if score >= 85 and self._has_anchor(job, match):
                return DedupeDecision(
                    is_duplicate=True,
                    canonical_id=match.id,
                    reason="fuzzy_anchor_merge",
                    score=score,
                    matched_job=match,
                )

        return DedupeDecision(is_duplicate=False, canonical_id=None, reason="unique")

    def _exact_keys(self, job: JobPosting) -> List[str]:
        keys = []
        if job.canonical_url:
            keys.append(f"url:{job.canonical_url}")
        for name, value in job.external_ids.items():
            keys.append(f"external:{name}:{value}")
        return keys

    def _best_fuzzy_match(
        self,
        job: JobPosting,
        candidates: Iterable[CanonicalJob],
    ) -> tuple[Optional[CanonicalJob], Optional[float]]:
        best_job: Optional[CanonicalJob] = None
        best_score: Optional[float] = None

        for candidate in candidates:
            company_score = fuzz.ratio(job.normalized_company, candidate.normalized_company)
            if company_score < 90:
                continue
            title_score = fuzz.token_set_ratio(job.normalized_title, candidate.normalized_title)
            if self._has_conflicting_specialization(job.normalized_title, candidate.normalized_title):
                continue
            location_score = fuzz.token_set_ratio(
                " ".join(job.normalized_locations),
                " ".join(candidate.normalized_locations),
            )
            season_score = 100 if (job.season_term or "") == (candidate.season_term or "") else 85
            score = (company_score * 0.35) + (title_score * 0.45) + (location_score * 0.10) + (season_score * 0.10)
            if best_score is None or score > best_score:
                best_job = candidate
                best_score = score

        return best_job, best_score

    def _has_anchor(self, job: JobPosting, candidate: CanonicalJob) -> bool:
        job_host = urlparse(job.canonical_url or job.url).netloc
        candidate_host = urlparse(candidate.canonical_url).netloc
        if job_host and job_host == candidate_host:
            return True

        if set(job.normalized_locations).intersection(set(candidate.normalized_locations)):
            return True

        if job.season_term and candidate.season_term and job.season_term == candidate.season_term:
            return True

        return False

    def _has_conflicting_specialization(self, left_title: str, right_title: str) -> bool:
        left = {token for token in SPECIALIZATION_TOKENS if token in left_title.split()}
        right = {token for token in SPECIALIZATION_TOKENS if token in right_title.split()}
        if not left or not right:
            return False
        return left != right and not left.intersection(right)
