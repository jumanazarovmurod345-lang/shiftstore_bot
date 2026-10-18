"""Двухэтапный поиск: pHash (близкие копии) -> эмбеддинги (визуально похожие).

Решение «товар найден» принимается только при выполнении настраиваемых условий:
  * этап 1: ровно один товар в пределах PHASH_MAX_DISTANCE;
  * этап 2: top1 >= MATCH_THRESHOLD и top1 - top2 >= MATCH_MARGIN,
    где top1/top2 — лучшие оценки двух РАЗНЫХ товаров (оценка товара = максимум
    косинусной близости по всем его фото).
Иначе — «неуверенно» (top1 >= MATCH_MIN_SCORE) или «не найден».
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Product, ProductImage

log = logging.getLogger(__name__)

Outcome = Literal["match", "uncertain", "not_found"]


@dataclass(slots=True)
class Candidate:
    product_id: int
    image_id: int
    score: float  # косинус для эмбеддингов; для pHash — 1 - d/64
    phash_distance: int | None = None


@dataclass(slots=True)
class MatchDecision:
    outcome: Outcome
    stage: Literal["phash", "embedding"]
    best: Candidate | None = None
    second: Candidate | None = None
    candidates: list[Candidate] = field(default_factory=list)


@dataclass(slots=True)
class Thresholds:
    match_threshold: float
    match_margin: float
    match_min_score: float
    phash_max_distance: int


def decide_phash(
    product_ids: np.ndarray, image_ids: np.ndarray, distances: np.ndarray, max_distance: int
) -> MatchDecision | None:
    """Возвращает решение 'match' только если ближайшая копия однозначна."""
    if distances.size == 0:
        return None
    best_by_product: dict[int, Candidate] = {}
    for pid, iid, d in zip(product_ids.tolist(), image_ids.tolist(), distances.tolist(), strict=True):
        if d > max_distance:
            continue
        cur = best_by_product.get(pid)
        if cur is None or d < (cur.phash_distance or 99):
            best_by_product[pid] = Candidate(pid, iid, 1 - d / 64, d)
    if len(best_by_product) != 1:
        # 0 — нет копий; >1 — одно фото в нескольких товарах: решает этап 2/админ
        return None
    (only,) = best_by_product.values()
    return MatchDecision("match", "phash", best=only, candidates=[only])


def decide_embedding(
    product_ids: np.ndarray, image_ids: np.ndarray, scores: np.ndarray, t: Thresholds, top_k: int = 5
) -> MatchDecision:
    best_by_product: dict[int, Candidate] = {}
    for pid, iid, s in zip(product_ids.tolist(), image_ids.tolist(), scores.tolist(), strict=True):
        cur = best_by_product.get(pid)
        if cur is None or s > cur.score:
            best_by_product[pid] = Candidate(pid, iid, float(s))
    ranked = sorted(best_by_product.values(), key=lambda c: c.score, reverse=True)
    if not ranked:
        return MatchDecision("not_found", "embedding")
    best = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None
    margin = best.score - (second.score if second else 0.0)
    if best.score >= t.match_threshold and margin >= t.match_margin:
        outcome: Outcome = "match"
    elif best.score >= t.match_min_score:
        outcome = "uncertain"
    else:
        outcome = "not_found"
    return MatchDecision(outcome, "embedding", best=best, second=second, candidates=ranked[:top_k])


@dataclass(slots=True)
class CatalogIndex:
    fingerprint: tuple[int, datetime | None, str]
    image_ids: np.ndarray
    product_ids: np.ndarray
    phashes: np.ndarray  # uint64
    phash_mask: np.ndarray  # bool: есть pHash
    emb_image_ids: np.ndarray
    emb_product_ids: np.ndarray
    matrix: np.ndarray  # (n, d) float32, строки L2-нормированы

    def phash_distances(self, query: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        q = np.uint64(query & 0xFFFFFFFFFFFFFFFF)
        hashes = self.phashes[self.phash_mask]
        d = np.bitwise_count(np.bitwise_xor(hashes, q)).astype(np.int64)
        return self.product_ids[self.phash_mask], self.image_ids[self.phash_mask], d

    def cosine(self, query: np.ndarray) -> np.ndarray:
        if self.matrix.size == 0:
            return np.zeros(0, dtype=np.float32)
        if query.shape[0] != self.matrix.shape[1]:
            raise ValueError("Размерность запроса не совпадает с индексом — нужна переиндексация")
        return self.matrix @ query


class CatalogIndexCache:
    """Кэш векторов каталога в памяти процесса; перечитывается при изменении каталога.

    Для небольшого каталога (до ~десятков тысяч фото) полный перебор матрицы
    занимает миллисекунды. Переход на pgvector/внешний векторный индекс — см. README.
    """

    def __init__(self) -> None:
        self._index: CatalogIndex | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _active_filter():  # noqa: ANN205
        return (Product.is_active.is_(True), ProductImage.is_active.is_(True))

    async def _fingerprint(self, session: AsyncSession, model: str) -> tuple[int, datetime | None, str]:
        row = (
            await session.execute(
                select(
                    func.count(ProductImage.id),
                    func.max(func.greatest(ProductImage.updated_at, Product.updated_at)),
                )
                .join(Product, Product.id == ProductImage.product_id)
                .where(*self._active_filter())
            )
        ).one()
        return int(row[0]), row[1], model

    async def get(self, session: AsyncSession, model: str) -> CatalogIndex:
        fp = await self._fingerprint(session, model)
        if self._index is not None and self._index.fingerprint == fp:
            return self._index
        async with self._lock:
            if self._index is not None and self._index.fingerprint == fp:
                return self._index
            rows = (
                await session.execute(
                    select(
                        ProductImage.id,
                        ProductImage.product_id,
                        ProductImage.phash,
                        ProductImage.embedding,
                        ProductImage.embedding_model,
                        ProductImage.embedding_dim,
                    )
                    .join(Product, Product.id == ProductImage.product_id)
                    .where(*self._active_filter())
                )
            ).all()
            image_ids = np.array([r[0] for r in rows], dtype=np.int64)
            product_ids = np.array([r[1] for r in rows], dtype=np.int64)
            phashes = np.array(
                [(r[2] or 0) & 0xFFFFFFFFFFFFFFFF for r in rows], dtype=np.uint64
            )
            mask = np.array([r[2] is not None for r in rows], dtype=bool)
            emb_rows = [r for r in rows if r[3] is not None and r[4] == model]
            dims = {r[5] for r in emb_rows}
            if len(dims) > 1:
                log.warning("mixed embedding dims in catalog", extra={"dims": sorted(dims)})
                common = max(dims, key=lambda d: sum(1 for r in emb_rows if r[5] == d))
                emb_rows = [r for r in emb_rows if r[5] == common]
            matrix = (
                np.vstack([np.frombuffer(r[3], dtype=np.float32) for r in emb_rows])
                if emb_rows
                else np.zeros((0, 0), dtype=np.float32)
            )
            self._index = CatalogIndex(
                fingerprint=fp,
                image_ids=image_ids,
                product_ids=product_ids,
                phashes=phashes,
                phash_mask=mask,
                emb_image_ids=np.array([r[0] for r in emb_rows], dtype=np.int64),
                emb_product_ids=np.array([r[1] for r in emb_rows], dtype=np.int64),
                matrix=matrix,
            )
            log.info(
                "catalog index loaded",
                extra={"images": len(rows), "with_embeddings": len(emb_rows)},
            )
            return self._index

    def invalidate(self) -> None:
        self._index = None
