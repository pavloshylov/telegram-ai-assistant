"""Database: users, conversation log (with token usage), leads, and the two demo catalogs.

SQLite by default; PostgreSQL via DATABASE_URL=postgresql+asyncpg://... with no code changes.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, delete
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str] = mapped_column(String(128), default="")
    language: Mapped[str | None] = mapped_column(String(8))
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Message(Base):
    """Conversation log. `meta` keeps what was shown (item ids, cited sources); tokens feed the cost report."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    text: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # viewing | order | human
    item_id: Mapped[str | None] = mapped_column(String(32))
    phone: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Listing(Base):
    __tablename__ = "listings"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    deal: Mapped[str] = mapped_column(String(8), index=True)  # rent | sale
    district: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(128))
    rooms: Mapped[int] = mapped_column(Integer)  # bedrooms; 0 = studio
    area_m2: Mapped[int] = mapped_column(Integer)
    price_eur: Mapped[int] = mapped_column(Integer, index=True)
    floor: Mapped[int] = mapped_column(Integer)
    furnished: Mapped[bool] = mapped_column(Boolean)
    pets: Mapped[bool] = mapped_column(Boolean)
    available_from: Mapped[str] = mapped_column(String(10))
    description: Mapped[str] = mapped_column(Text)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(128))
    brand: Mapped[str] = mapped_column(String(64))
    price_eur: Mapped[float] = mapped_column(Float, index=True)
    stock: Mapped[int] = mapped_column(Integer)
    anc: Mapped[bool] = mapped_column(Boolean)
    water_rating: Mapped[str] = mapped_column(String(8))
    battery_h: Mapped[int] = mapped_column(Integer)
    multipoint: Mapped[bool] = mapped_column(Boolean)
    warranty_years: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)


def make_engine(database_url: str) -> AsyncEngine:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    if url.get_backend_name() == "sqlite":  # several widget visitors write at once: wait for the lock, don't fail
        return create_async_engine(database_url, connect_args={"timeout": 30})
    return create_async_engine(database_url, pool_pre_ping=True)


def make_session_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _bool(value: str) -> bool:
    return value.strip().lower() in {"yes", "true", "1"}


async def load_catalog(session: AsyncSession, model: type[Listing] | type[Product], csv_path: Path) -> int:
    """Replaces the catalog table with the CSV contents (the CSV is the source of truth for the demo)."""
    await session.execute(delete(model))
    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        values: dict = {}
        for column in model.__table__.columns:
            raw = row[column.name]
            python_type = column.type.python_type
            values[column.name] = _bool(raw) if python_type is bool else python_type(raw)
        session.add(model(**values))
    await session.flush()
    return len(rows)
