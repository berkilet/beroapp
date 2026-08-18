"""SQLAlchemy declarative base and shared column conventions."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData
from sqlalchemy.orm import DeclarativeBase, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    return datetime.now(UTC)


def ts_column(**kwargs: object):
    """Timezone-aware timestamp column.

    Every timestamp in this system is stored with a timezone. A naive timestamp
    in a system that reasons about what was known when is a latent bug.
    """
    return mapped_column(DateTime(timezone=True), **kwargs)  # type: ignore[arg-type]
