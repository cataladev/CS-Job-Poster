from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from job_bot.database import Database
from job_bot.models import AppConfig, SourceResult, SourceSettings
from job_bot.utils.http import AsyncHttpClient


@dataclass
class SourceContext:
    config: AppConfig
    database: Database
    http_client: AsyncHttpClient


class BaseSource(ABC):
    phase = "secondary"

    def __init__(self, settings: SourceSettings) -> None:
        self.settings = settings
        self.logger = logging.getLogger(f"source.{settings.name}")

    @property
    def name(self) -> str:
        return self.settings.name

    @property
    def track(self) -> str:
        return self.settings.track

    @property
    def priority(self) -> int:
        return self.settings.priority

    @property
    def experimental(self) -> bool:
        return self.settings.experimental

    def is_enabled(self, context: SourceContext) -> bool:
        return self.settings.enabled

    @abstractmethod
    async def fetch(self, context: SourceContext) -> SourceResult:
        raise NotImplementedError
