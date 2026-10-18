"""Проверка изображений и perceptual hash (pHash, DCT) без тяжёлых зависимостей."""

from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}
MAX_PIXELS = 40_000_000  # защита от decompression bomb
MIN_SIDE = 32


class InvalidImageError(ValueError):
    """Файл не является допустимым изображением (сообщение — для пользователя)."""


@dataclass(slots=True)
class LoadedImage:
    image: Image.Image  # RGB, ориентация по EXIF уже применена
    format: str
    width: int
    height: int


def load_image(data: bytes, max_bytes: int) -> LoadedImage:
    if len(data) > max_bytes:
        raise InvalidImageError("too_large")
    if not data:
        raise InvalidImageError("empty")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = probe.format or ""
            w, h = probe.size
            probe.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise InvalidImageError("corrupted") from exc
    if fmt not in ALLOWED_FORMATS:
        raise InvalidImageError("format")
    if w * h > MAX_PIXELS:
        raise InvalidImageError("too_many_pixels")
    if min(w, h) < MIN_SIDE:
        raise InvalidImageError("too_small")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
    except (OSError, ValueError) as exc:
        raise InvalidImageError("corrupted") from exc
    return LoadedImage(image=img, format=fmt, width=img.width, height=img.height)


def _dct_matrix(n: int) -> np.ndarray:
    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    m = np.cos(np.pi * (2 * i + 1) * k / (2 * n)) * np.sqrt(2.0 / n)
    m[0, :] = np.sqrt(1.0 / n)
    return m


_DCT32 = _dct_matrix(32)


def phash(img: Image.Image) -> int:
    """64-битный pHash, возвращается как знаковое int64 (под BIGINT в PostgreSQL)."""
    gray = img.convert("L").resize((32, 32), Image.Resampling.LANCZOS)
    pixels = np.asarray(gray, dtype=np.float64)
    dct = _DCT32 @ pixels @ _DCT32.T
    low = dct[:8, :8].flatten()
    median = np.median(low[1:])  # без DC-коэффициента
    bits = low > median
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    if value >= 1 << 63:
        value -= 1 << 64
    return value


def hamming(a: int, b: int) -> int:
    return ((a ^ b) & 0xFFFFFFFFFFFFFFFF).bit_count()


def prepare_for_embedding(img: Image.Image, max_side: int) -> bytes:
    """Уменьшает изображение (стоимость API считается по пикселям) и кодирует в JPEG."""
    copy = img.copy()
    copy.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    copy.save(buf, format="JPEG", quality=90)
    return buf.getvalue()
