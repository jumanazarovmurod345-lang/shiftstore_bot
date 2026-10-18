"""Конфигурация приложения из переменных окружения."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


def normalize_database_url(url: str) -> str:
    """Приводит DATABASE_URL Railway (postgresql://...) к драйверу asyncpg.

    asyncpg не понимает параметр ``sslmode`` — переводим его в ``ssl``.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    if "sslmode" in query:
        query["ssl"] = query.pop("sslmode")
    return urlunsplit(parts._replace(query=urlencode(query)))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram ---
    bot_token: SecretStr = Field(alias="BOT_TOKEN")
    channel_id: int = Field(alias="CHANNEL_ID")
    admin_id: int | None = Field(default=None, alias="ADMIN_ID")
    admin_chat_id: int | None = Field(default=None, alias="ADMIN_CHAT_ID")
    admin_ids_raw: str | None = Field(default=None, alias="ADMIN_IDS")
    bot_mode: Literal["webhook", "polling"] = Field(default="webhook", alias="BOT_MODE")
    app_base_url: str | None = Field(default=None, alias="APP_BASE_URL")
    webhook_secret: SecretStr | None = Field(default=None, alias="WEBHOOK_SECRET")
    polling_delete_webhook: bool = Field(default=False, alias="POLLING_DELETE_WEBHOOK")

    # --- Инфраструктура ---
    database_url: str = Field(alias="DATABASE_URL")
    port: int = Field(default=8000, alias="PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    db_pool_size: int = Field(default=5, alias="DB_POOL_SIZE")

    # --- Фоновая очередь ---
    worker_concurrency: int = Field(default=4, ge=1, le=32, alias="WORKER_CONCURRENCY")
    job_lease_seconds: int = Field(default=120, ge=15, alias="JOB_LEASE_SECONDS")
    shutdown_grace_seconds: int = Field(default=20, ge=1, alias="SHUTDOWN_GRACE_SECONDS")

    # --- Поиск по изображению ---
    image_embedding_provider: Literal["voyage", "none"] = Field(
        default="voyage", alias="IMAGE_EMBEDDING_PROVIDER"
    )
    image_embedding_api_key: SecretStr | None = Field(default=None, alias="IMAGE_EMBEDDING_API_KEY")
    image_embedding_model: str = Field(default="voyage-multimodal-3.5", alias="IMAGE_EMBEDDING_MODEL")
    image_embedding_base_url: str = Field(
        default="https://api.voyageai.com/v1", alias="IMAGE_EMBEDDING_BASE_URL"
    )
    image_embedding_timeout: float = Field(default=30.0, alias="IMAGE_EMBEDDING_TIMEOUT")
    image_embedding_max_side: int = Field(default=1024, ge=256, le=4096, alias="IMAGE_EMBEDDING_MAX_SIDE")

    match_threshold: float = Field(default=0.86, ge=0, le=1, alias="MATCH_THRESHOLD")
    match_margin: float = Field(default=0.03, ge=0, le=1, alias="MATCH_MARGIN")
    match_min_score: float = Field(default=0.70, ge=0, le=1, alias="MATCH_MIN_SCORE")
    phash_max_distance: int = Field(default=6, ge=0, le=32, alias="PHASH_MAX_DISTANCE")
    max_image_size_mb: float = Field(default=10.0, gt=0, alias="MAX_IMAGE_SIZE_MB")

    # --- Прочее ---
    timezone: str = Field(default="Asia/Tashkent", alias="TIMEZONE")
    caption_parser: str = Field(default="default", alias="CAPTION_PARSER")
    notify_admin_on_product: bool = Field(default=True, alias="NOTIFY_ADMIN_ON_PRODUCT")

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        if not self.app_base_url and os.getenv("RAILWAY_PUBLIC_DOMAIN"):
            self.app_base_url = "https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"]
        if self.webhook_secret is not None and not self.webhook_secret.get_secret_value():
            self.webhook_secret = None
        if self.image_embedding_api_key is not None and not self.image_embedding_api_key.get_secret_value():
            self.image_embedding_api_key = None

        if self.bot_mode == "webhook":
            if not self.app_base_url or not self.app_base_url.startswith("https://"):
                raise ValueError("В режиме webhook APP_BASE_URL обязателен и должен начинаться с https://")
            if self.webhook_secret is None:
                raise ValueError("В режиме webhook обязателен WEBHOOK_SECRET")
        if self.webhook_secret is not None and not _SECRET_RE.match(
            self.webhook_secret.get_secret_value()
        ):
            raise ValueError("WEBHOOK_SECRET: 1-256 символов A-Z, a-z, 0-9, _ и -")
        if self.match_min_score > self.match_threshold:
            raise ValueError("MATCH_MIN_SCORE не может быть больше MATCH_THRESHOLD")

        # --- Валидация и нормализация ADMIN_ID ---
        if self.admin_id is not None:
            if self.admin_chat_id is None:
                self.admin_chat_id = self.admin_id
            if not self.admin_ids_raw:
                self.admin_ids_raw = str(self.admin_id)

        if self.admin_chat_id is None and not self.admin_ids_raw:
            raise ValueError(
                "ADMIN_ID не настроен! Задайте числовой Telegram ID администратора в переменной ADMIN_ID "
                "(или ADMIN_CHAT_ID и ADMIN_IDS) в файле .env"
            )

        if self.admin_chat_id is None:
            ids = [int(x) for x in re.split(r"[,\s]+", (self.admin_ids_raw or "").strip()) if x]
            if not ids:
                raise ValueError("ADMIN_IDS пуст. Задайте ADMIN_ID в файле .env")
            self.admin_chat_id = ids[0]

        if not self.admin_ids_raw:
            self.admin_ids_raw = str(self.admin_chat_id)

        _ = self.admin_ids  # валидация формата
        return self

    @property
    def admin_ids(self) -> frozenset[int]:
        if not self.admin_ids_raw:
            return frozenset({self.admin_chat_id}) if self.admin_chat_id else frozenset()
        try:
            return frozenset(int(x) for x in re.split(r"[,\s]+", self.admin_ids_raw.strip()) if x)
        except ValueError as exc:  # pragma: no cover - сообщение для оператора
            raise ValueError("ADMIN_IDS: список числовых Telegram ID через запятую") from exc

    @property
    def async_database_url(self) -> str:
        return normalize_database_url(self.database_url)

    @property
    def webhook_url(self) -> str:
        return f"{self.app_base_url}/telegram/webhook"

    @property
    def max_image_bytes(self) -> int:
        # Bot API getFile не отдаёт файлы больше 20 МБ.
        return int(min(self.max_image_size_mb, 20.0) * 1024 * 1024)

    @property
    def embeddings_enabled(self) -> bool:
        return self.image_embedding_provider != "none" and self.image_embedding_api_key is not None

    def secrets(self) -> list[str]:
        out = [self.bot_token.get_secret_value()]
        if self.webhook_secret:
            out.append(self.webhook_secret.get_secret_value())
        if self.image_embedding_api_key:
            out.append(self.image_embedding_api_key.get_secret_value())
        return [s for s in out if s]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
