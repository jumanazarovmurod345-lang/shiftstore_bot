"""Парсер подписей к постам каталога.

Интерфейс ``CaptionParser`` позволяет подменить реализацию (например, под другой
формат постов) через переменную CAPTION_PARSER. Парсер НИЧЕГО не додумывает:
если поле явно не указано — оно остаётся ``None``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Protocol


@dataclass(slots=True)
class ParsedCaption:
    name: str | None = None
    sku: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    sizes: list[str] | None = None
    availability: str | None = None
    in_stock: bool | None = None
    unknown_lines: list[str] = field(default_factory=list)


class CaptionParser(Protocol):
    def parse(self, text: str | None) -> ParsedCaption: ...


_CURRENCIES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(uzs|сум|сўм|сумм|so['ʻ’`]?m|sum)\.?$", re.I), "UZS"),
    (re.compile(r"^(usd|\$|долл\w*|у\.?е\.?)$", re.I), "USD"),
    (re.compile(r"^(rub|руб\w*|₽|р\.?)$", re.I), "RUB"),
    (re.compile(r"^(eur|€|евро)$", re.I), "EUR"),
]

_KEYS: dict[str, tuple[str, ...]] = {
    "name": ("название", "наименование", "товар", "модель", "nomi"),
    "sku": ("артикул", "арт", "код", "sku", "article"),
    "price": ("цена", "стоимость", "narxi", "narx", "price"),
    "sizes": ("размеры", "размер", "размерный ряд", "o'lcham", "olcham", "sizes", "size"),
    "availability": ("наличие", "в наличии", "статус", "mavjud"),
}

_LINE_RE = re.compile(r"^\s*[^\wА-Яа-яЁё]*\s*([\w\sА-Яа-яЁё'ʻ’]{2,30}?)\s*[:：\-–—]\s*(.+?)\s*$")
_NUM_RE = re.compile(r"(\d[\d\s\u00a0\u202f.,']*)")
_EMOJI_PREFIX = re.compile(r"^[^\wА-Яа-яЁё$€₽]+")


def normalize_currency(token: str) -> str | None:
    token = token.strip().strip(".,;")
    for pattern, code in _CURRENCIES:
        if pattern.match(token):
            return code
    return None


def parse_amount(raw: str) -> Decimal | None:
    """'650 000' / '650.000' / '650,000' / '1 250 000.50' / '49.99' -> Decimal."""
    s = raw.strip().replace("\u00a0", " ").replace("\u202f", " ").replace("'", " ")
    s = re.sub(r"\s+", "", s).rstrip(".,")
    if not s:
        return None
    # Разделители групп по 3 цифры: 650.000 / 1,250,000 / 1.250.000,50
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", s):
        s = re.sub(r"[.,]", "", s)
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+,\d{1,2}", s):
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(,\d{3})+\.\d{1,2}", s):
        s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    if value <= 0 or value >= Decimal("1e13"):
        return None
    return value.quantize(Decimal("0.01"))


def parse_price(value: str) -> tuple[Decimal | None, str | None]:
    m = _NUM_RE.search(value)
    if not m:
        return None, None
    amount = parse_amount(m.group(1))
    before = value[: m.start()].strip().split()
    after = value[m.end() :].strip().split()
    currency = None
    for tok in (after[:1] + before[-1:]):
        currency = normalize_currency(tok)
        if currency:
            break
    if currency is None and "$" in value:
        currency = "USD"
    return amount, currency


def parse_sizes(value: str) -> list[str] | None:
    parts = [p.strip() for p in re.split(r"[,;/|]+|\s{2,}|\s+(?=\d)", value) if p.strip()]
    sizes: list[str] = []
    for p in parts:
        p = p.strip(" .")
        range_m = re.fullmatch(r"(\d{2})\s*[-–—]\s*(\d{2})", p)
        if range_m:
            lo, hi = int(range_m.group(1)), int(range_m.group(2))
            if lo < hi and hi - lo <= 15:
                sizes.extend(str(x) for x in range(lo, hi + 1))
                continue
        if p and len(p) <= 12:
            sizes.append(p)
    # уникальные с сохранением порядка
    seen: set[str] = set()
    result = [s for s in sizes if not (s in seen or seen.add(s))]
    return result or None


def parse_availability(value: str) -> tuple[str, bool | None]:
    v = value.strip()
    low = v.lower()
    if re.search(r"\b(нет|отсутств|закончил|распродан|sold\s*out|yo['ʻ’]?q)", low):
        return v, False
    if re.search(r"(в наличии|есть|available|mavjud|in stock|под заказ)", low):
        return v, "под заказ" not in low or None
    return v, None


class KeyValueCaptionParser:
    """Формат «Ключ: значение» по строкам + аккуратный fallback для названия."""

    def _key_of(self, label: str) -> str | None:
        label = re.sub(r"\s+", " ", label.strip().lower())
        for key, aliases in _KEYS.items():
            if label in aliases:
                return key
        return None

    def parse(self, text: str | None) -> ParsedCaption:
        result = ParsedCaption()
        if not text or not text.strip():
            return result
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        for line in lines:
            m = _LINE_RE.match(line)
            key = self._key_of(m.group(1)) if m else None
            if not m or key is None:
                result.unknown_lines.append(line)
                continue
            value = m.group(2).strip()
            if key == "name" and result.name is None:
                result.name = value[:512]
            elif key == "sku" and result.sku is None:
                result.sku = value[:128]
            elif key == "price" and result.price is None:
                result.price, result.currency = parse_price(value)
            elif key == "sizes" and result.sizes is None:
                result.sizes = parse_sizes(value)
            elif key == "availability" and result.availability is None:
                result.availability, result.in_stock = parse_availability(value)

        # Fallback: название = первая содержательная строка (без цены/хештегов/ссылок).
        if result.name is None:
            for line in result.unknown_lines:
                cleaned = _EMOJI_PREFIX.sub("", line).strip()
                if (
                    len(cleaned) >= 3
                    and not cleaned.startswith(("#", "http", "@"))
                    and not _NUM_RE.fullmatch(cleaned)
                ):
                    result.name = cleaned[:512]
                    break
        return result


_PARSERS: dict[str, type] = {"default": KeyValueCaptionParser}


def get_caption_parser(name: str = "default") -> CaptionParser:
    try:
        return _PARSERS[name]()
    except KeyError as exc:
        raise ValueError(f"Неизвестный CAPTION_PARSER: {name}") from exc
