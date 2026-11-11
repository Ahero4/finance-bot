from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy import Integer, BigInteger, String, DateTime, CheckConstraint
from datetime import datetime

from pathlib import Path
import os

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///" + (Path(__file__).resolve().parent.parent / "db.sqlite").as_posix()
engine = create_async_engine(os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL)
async_session = async_sessionmaker(engine)


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Expense(Base):
    __tablename__ = "expenses"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id = mapped_column(BigInteger)
    sum: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(255))
    date: Mapped[datetime] = mapped_column(DateTime)


class UserSettings(Base):
    __tablename__ = "user_settings"
    __table_args__ = (CheckConstraint("currency IN ('RUB', 'USD')"),)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="RUB", server_default="RUB")


async def async_main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
