from __future__ import annotations

from typing import Dict, List, Type

from job_bot.config import AppConfig
from job_bot.models import SourceSettings
from job_bot.sources.ashby import AshbySource
from job_bot.sources.base import BaseSource
from job_bot.sources.company_pages import CompanyPagesSource
from job_bot.sources.github_simplify import GitHubSimplifySource
from job_bot.sources.greenhouse import GreenhouseSource
from job_bot.sources.lever import LeverSource
from job_bot.sources.wellfound import WellfoundSource
from job_bot.sources.workday import WorkdaySource
from job_bot.sources.yc import YCombinatorSource


SOURCE_REGISTRY: Dict[str, Type[BaseSource]] = {
    "github_simplify": GitHubSimplifySource,
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "ashby": AshbySource,
    "workday": WorkdaySource,
    "yc": YCombinatorSource,
    "wellfound": WellfoundSource,
    "company_pages": CompanyPagesSource,
}


def build_sources(config: AppConfig) -> List[BaseSource]:
    sources: List[BaseSource] = []
    for name, settings in config.source_settings.items():
        source_cls = SOURCE_REGISTRY.get(settings.source_type)
        if source_cls is None:
            continue
        sources.append(source_cls(settings))
    return sorted(sources, key=lambda source: source.priority)
