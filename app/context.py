"""Общий контекст: зависимости, доступные обработчикам апдейтов и задач."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.catalog.parser import CaptionParser
from app.config import Settings
from app.search.embeddings import ImageEmbeddingProvider
from app.search.matcher import CatalogIndexCache, Thresholds

if TYPE_CHECKING:
    from aiogram import Dispatcher


@dataclass
class AppContext:
    settings: Settings
    bot: Bot
    sessionmaker: async_sessionmaker[AsyncSession]
    provider: ImageEmbeddingProvider
    parser: CaptionParser
    index_cache: CatalogIndexCache = field(default_factory=CatalogIndexCache)
    dp: Dispatcher | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.settings.timezone)

    @property
    def thresholds(self) -> Thresholds:
        s = self.settings
        return Thresholds(s.match_threshold, s.match_margin, s.match_min_score, s.phash_max_distance)

    @property
    def embeddings_enabled(self) -> bool:
        return self.provider.name != "none"

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.settings.admin_ids
