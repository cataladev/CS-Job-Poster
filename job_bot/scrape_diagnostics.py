from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import traceback
from typing import Any, AsyncIterator, Literal


@dataclass(frozen=True)
class ScrapeSourceResult:
    source_id: str
    label: str
    status: Literal["fetched", "unchanged", "skipped", "failed", "not_run"] = "not_run"
    observations: int = 0
    skip_reason: str = "not due"
    # Earliest eligible check; the reporter aligns it to the polling loop.
    next_due_at: datetime | None = None
    new_jobs: int = 0
    provider_label: str = ""
    duplicates: int = 0


@dataclass(frozen=True)
class ScrapeFailure:
    source: str | None
    stage: str
    error_type: str
    message: str
    location: str = ""

    @classmethod
    def from_exception(
        cls, error: BaseException, *, source: str | None = None, stage: str = "run"
    ) -> ScrapeFailure:
        existing = getattr(error, "scrape_failure", None)
        if isinstance(existing, cls):
            return existing
        frames = traceback.extract_tb(error.__traceback__)
        project_root = Path(__file__).resolve().parent.parent
        locations = []
        for frame in frames:
            try:
                relative = Path(frame.filename).resolve().relative_to(project_root)
            except ValueError:
                continue
            if relative.parts[0] == "job_bot" and relative.name != "scrape_diagnostics.py":
                locations.append(f"{relative}:{frame.lineno} in {frame.name}")
        return cls(
            source=source,
            stage=stage,
            error_type=type(error).__name__,
            message=str(error) or "No error message supplied",
            location=" → ".join(locations[-2:]),
        )

    def describe(self) -> str:
        source = self.source or "Shared pipeline (no individual scraper)"
        heading = f"{source} • {self.stage}" if self.stage else source
        error = f"{self.error_type}: {self.message}" if self.error_type else self.message
        detail = f"{heading}\n{error}"
        if self.location:
            detail += f"\nLocation: {self.location}"
        return detail


@dataclass
class ScrapeContext:
    stage: str
    source: str | None = None


@asynccontextmanager
async def scrape_context(
    stage: str, *, source: str | None = None, stats: Any | None = None
) -> AsyncIterator[ScrapeContext]:
    """Keep the original exception and attach context before it leaves a task.

    Each concurrent source owns its context. The outer run also preserves partial
    counters and source failures if a later database or publisher operation fails.
    """
    context = ScrapeContext(stage=stage, source=source)
    try:
        yield context
    except Exception as error:
        error.scrape_failure = ScrapeFailure.from_exception(  # type: ignore[attr-defined]
            error, source=context.source, stage=context.stage
        )
        if stats is not None:
            error.scrape_stats = stats  # type: ignore[attr-defined]
        raise
