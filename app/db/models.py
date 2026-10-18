"""ORM-модели. Схема создаётся только миграциями Alembic (см. alembic/versions)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    first_name: Mapped[str | None] = mapped_column(String(256))
    last_name: Mapped[str | None] = mapped_column(String(256))
    username: Mapped[str | None] = mapped_column(String(64))
    phone: Mapped[str | None] = mapped_column(String(32))
    phone_shared: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    phone_refused: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phone_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Product(TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("source_chat_id", "group_key", name="uq_products_source_group"),
        Index("ix_products_active", "is_active"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # "msg:<message_id>" | "mg:<media_group_id>" | "fwd:<admin_chat>:<media_group_id>"
    group_key: Mapped[str] = mapped_column(String(128), nullable=False)
    media_group_id: Mapped[str | None] = mapped_column(String(64))
    first_message_id: Mapped[int | None] = mapped_column(BigInteger)
    caption_message_id: Mapped[int | None] = mapped_column(BigInteger)
    post_url: Mapped[str | None] = mapped_column(String(512))
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="channel")

    raw_caption: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(String(512))
    sku: Mapped[str | None] = mapped_column(String(128))
    price: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    currency: Mapped[str | None] = mapped_column(String(8))
    sizes: Mapped[list[str] | None] = mapped_column(JSONB)
    availability: Mapped[str | None] = mapped_column(String(256))
    in_stock: Mapped[bool | None] = mapped_column(Boolean)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deactivated_by: Mapped[int | None] = mapped_column(BigInteger)

    images: Mapped[list[ProductImage]] = relationship(
        back_populates="product", order_by="ProductImage.source_message_id", lazy="selectin"
    )


class ProductImage(TimestampMixin, Base):
    __tablename__ = "product_images"
    __table_args__ = (
        UniqueConstraint("source_chat_id", "source_message_id", name="uq_images_source_message"),
        Index("ix_images_product", "product_id"),
        Index("ix_images_index_status", "index_status"),
        Index("ix_images_file_unique", "file_unique_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    source_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_id: Mapped[str] = mapped_column(String(256), nullable=False)
    file_unique_id: Mapped[str] = mapped_column(String(128), nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    phash: Mapped[int | None] = mapped_column(BigInteger)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary)  # float32, L2-нормирован
    embedding_dim: Mapped[int | None] = mapped_column(Integer)
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    # pending | processing | ready | failed
    index_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    index_error: Mapped[str | None] = mapped_column(String(512))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    product: Mapped[Product] = relationship(back_populates="images")


class Request(TimestampMixin, Base):
    """Заявка клиента, переданная администратору."""

    __tablename__ = "requests"
    __table_args__ = (
        UniqueConstraint("source_chat_id", "source_message_id", name="uq_requests_source_message"),
        Index("ix_requests_status", "status"),
        Index("ix_requests_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    source_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    photo_file_id: Mapped[str] = mapped_column(String(256), nullable=False)
    photo_file_unique_id: Mapped[str | None] = mapped_column(String(128))
    photo_is_document: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    client_caption: Mapped[str | None] = mapped_column(Text)
    # not_found | uncertain | search_unavailable | index_incomplete
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    candidates: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    # new | in_progress | closed
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="new")
    assigned_admin_id: Mapped[int | None] = mapped_column(BigInteger)
    admin_photo_message_id: Mapped[int | None] = mapped_column(BigInteger)
    admin_message_id: Mapped[int | None] = mapped_column(BigInteger)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(lazy="joined")


class AdminReply(TimestampMixin, Base):
    __tablename__ = "admin_replies"
    __table_args__ = (
        UniqueConstraint("prompt_chat_id", "prompt_message_id", name="uq_admin_replies_prompt"),
        Index("ix_admin_replies_request", "request_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("requests.id", ondelete="CASCADE"), nullable=False
    )
    admin_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    prompt_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    prompt_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    # awaiting | sent | failed | cancelled
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="awaiting")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProcessedUpdate(Base):
    __tablename__ = "processed_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    update_type: Mapped[str | None] = mapped_column(String(32))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_pick", "status", "run_at"),
        Index("ix_jobs_kind", "kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    dedup_key: Mapped[str | None] = mapped_column(String(256), unique=True)
    # pending | running | done | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="8")
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    locked_by: Mapped[str | None] = mapped_column(String(64))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SearchLog(Base):
    """Технический журнал поисков — для калибровки порогов (без текста и телефонов)."""

    __tablename__ = "search_logs"
    __table_args__ = (Index("ix_search_logs_created", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    photo_file_unique_id: Mapped[str | None] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(16))
    top1_product_id: Mapped[int | None] = mapped_column(BigInteger)
    top1_score: Mapped[float | None] = mapped_column()
    top2_product_id: Mapped[int | None] = mapped_column(BigInteger)
    top2_score: Mapped[float | None] = mapped_column()
    phash_distance: Mapped[int | None] = mapped_column(Integer)
    model: Mapped[str | None] = mapped_column(String(128))
    request_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
