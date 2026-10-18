"""Провайдеры эмбеддингов изображений.

Реализован Voyage AI multimodal (POST /v1/multimodalembeddings, вход ``image_base64``).
Это именно мультимодальный endpoint, принимающий изображения, а не текстовый API.
Новый провайдер добавляется реализацией протокола ``ImageEmbeddingProvider``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import random
from typing import Literal, Protocol

import httpx
import numpy as np

from app.config import Settings

log = logging.getLogger(__name__)

Purpose = Literal["document", "query"]


class EmbeddingError(RuntimeError):
    """Временный или постоянный сбой провайдера эмбеддингов."""


class EmbeddingNotConfigured(EmbeddingError):
    """Провайдер не настроен (нет ключа или выбран 'none')."""


class ImageEmbeddingProvider(Protocol):
    name: str
    model: str

    async def embed_images(self, images: list[bytes], purpose: Purpose) -> list[np.ndarray]:
        """Возвращает L2-нормированные float32-векторы, по одному на изображение (JPEG)."""
        ...

    async def aclose(self) -> None: ...


def l2_normalize(vec: np.ndarray) -> np.ndarray:
    vec = vec.astype(np.float32)
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        raise EmbeddingError("Провайдер вернул нулевой вектор")
    return vec / norm


class VoyageMultimodalProvider:
    name = "voyage"

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://api.voyageai.com/v1",
        timeout: float = 30.0,
        max_retries: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self._url = base_url.rstrip("/") + "/multimodalembeddings"
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._max_retries = max_retries
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def embed_images(self, images: list[bytes], purpose: Purpose) -> list[np.ndarray]:
        if not images:
            return []
        body = {
            "model": self.model,
            # Одинаковый input_type для каталога и запроса: сравнение «картинка ↔ картинка».
            "input_type": "document",
            "truncation": True,
            "inputs": [
                {
                    "content": [
                        {
                            "type": "image_base64",
                            "image_base64": "data:image/jpeg;base64,"
                            + base64.b64encode(img).decode("ascii"),
                        }
                    ]
                }
                for img in images
            ],
        }
        delay = 1.0
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = await self._client.post(self._url, json=body, headers=self._headers)
            except httpx.HTTPError as exc:
                err: Exception = EmbeddingError(f"Сетевая ошибка Voyage: {type(exc).__name__}")
            else:
                if resp.status_code == 200:
                    data = resp.json().get("data", [])
                    if len(data) != len(images):
                        raise EmbeddingError("Voyage вернул неожиданное число векторов")
                    data.sort(key=lambda d: d.get("index", 0))
                    return [l2_normalize(np.asarray(d["embedding"], dtype=np.float32)) for d in data]
                if resp.status_code in (401, 403):
                    raise EmbeddingNotConfigured(f"Voyage отклонил ключ (HTTP {resp.status_code})")
                if resp.status_code not in (429, 500, 502, 503, 504):
                    raise EmbeddingError(f"Voyage HTTP {resp.status_code}: {resp.text[:200]}")
                err = EmbeddingError(f"Voyage HTTP {resp.status_code}")
            if attempt == self._max_retries:
                raise err
            log.warning("embedding retry", extra={"attempt": attempt, "error": str(err)})
            await asyncio.sleep(delay + random.uniform(0, 0.5))
            delay *= 2
        raise EmbeddingError("unreachable")  # pragma: no cover

    async def aclose(self) -> None:
        await self._client.aclose()


class DisabledProvider:
    name = "none"
    model = "none"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def embed_images(self, images: list[bytes], purpose: Purpose) -> list[np.ndarray]:
        raise EmbeddingNotConfigured(self.reason)

    async def aclose(self) -> None:
        return None


def make_provider(settings: Settings) -> ImageEmbeddingProvider:
    if settings.image_embedding_provider == "none":
        return DisabledProvider("IMAGE_EMBEDDING_PROVIDER=none")
    if settings.image_embedding_api_key is None:
        log.error(
            "IMAGE_EMBEDDING_API_KEY не задан: визуальный поиск (этап 2) отключён, "
            "работает только поиск точных копий по pHash"
        )
        return DisabledProvider("IMAGE_EMBEDDING_API_KEY не задан")
    if settings.image_embedding_provider == "voyage":
        return VoyageMultimodalProvider(
            api_key=settings.image_embedding_api_key.get_secret_value(),
            model=settings.image_embedding_model,
            base_url=settings.image_embedding_base_url,
            timeout=settings.image_embedding_timeout,
        )
    raise ValueError(f"Неизвестный провайдер: {settings.image_embedding_provider}")
