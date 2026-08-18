"""Evidence persistence.

Append-only. A revision is a new row plus a ``superseded_by_id`` pointer on the
old one; an existing row's value is never rewritten. That matters more than it
might appear: economic statistics are revised routinely, and a system that
overwrites July's payrolls with the revised figure would, on replay, "know" the
revision in July. Keeping both rows is what makes revised-data leakage
detectable rather than invisible.

Deduplication is on ``(source_id, content_hash)``, and the hash covers the fact
rather than the fetch — so re-collecting an unchanged observation is a no-op
while a changed value for the same period is stored as a new row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.enums import EvidenceType
from app.db.models import ExternalEvent, ExternalSource
from app.evidence.base import EvidenceItem


@dataclass
class StoreReport:
    inserted: int = 0
    duplicates: int = 0
    revisions: int = 0
    rejected: int = 0
    reasons: list[str] = field(default_factory=list)

    def reject(self, reason: str) -> None:
        self.rejected += 1
        if len(self.reasons) < 20:
            self.reasons.append(reason[:200])

    def as_dict(self) -> dict:
        return {
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "revisions": self.revisions,
            "rejected": self.rejected,
            "reasons": self.reasons,
        }


def store_items(
    session: Session,
    items: list[EvidenceItem],
    *,
    source_row_ids: dict[str, int],
    max_age_days: int = 400,
    now: datetime | None = None,
) -> tuple[StoreReport, list[int]]:
    """Persist evidence, returning the report and the ids actually written."""
    now = now or datetime.now(UTC)
    report = StoreReport()
    written: list[int] = []
    cutoff = now - timedelta(days=max_age_days)

    for item in items:
        source_id = source_row_ids.get(item.source_key)
        if source_id is None:
            report.reject(f"{item.series_key}: source {item.source_key} is not registered")
            continue

        if item.observation_date is not None and item.observation_date < cutoff:
            # Ancient history is not evidence about a live market, and storing
            # it would bloat the matcher's search space for no benefit.
            report.reject(f"{item.series_key}: observation older than {max_age_days} days")
            continue

        if item.known_at > now + timedelta(minutes=5):
            # A known_at in the future would let a backtest see it early.
            report.reject(f"{item.series_key}: known_at is in the future")
            continue

        stmt = (
            pg_insert(ExternalEvent)
            .values(
                source_id=source_id,
                market_id=None,
                source_type=item.source_type.value,
                source_tier=item.source_tier,
                evidence_type=item.evidence_type.value,
                series_key=item.series_key,
                numeric_value=item.numeric_value,
                unit=item.unit,
                observation_date=item.observation_date,
                reference_url=item.reference_url,
                title=item.title[:2000],
                payload={
                    **item.payload,
                    "subject_tags": list(item.subject_tags),
                    "categories": [c.value for c in item.categories],
                    "subcategories": [s.value for s in item.subcategories],
                },
                published_at=item.published_at,
                ingested_at=now,
                known_at=item.known_at,
                verification_status=item.verification_status.value,
                reliability_score=item.reliability_score,
                parser_version=item.parser_version,
                content_hash=item.content_hash,
            )
            # Same fact, already stored: nothing to do. This is what makes the
            # collector safe to run more often than the data changes.
            .on_conflict_do_nothing(index_elements=["source_id", "content_hash"])
            .returning(ExternalEvent.id)
        )
        new_id = session.execute(stmt).scalar_one_or_none()

        if new_id is None:
            report.duplicates += 1
            continue

        report.inserted += 1
        written.append(new_id)

        if _mark_superseded(session, item, source_id, new_id):
            report.revisions += 1

    return report, written


def _mark_superseded(
    session: Session, item: EvidenceItem, source_id: int, new_id: int
) -> bool:
    """Point earlier observations of the same fact at their replacement.

    Only applies to numeric time-series observations: a scheduled event or a
    filing is not "revised", it is simply another record.
    """
    if item.evidence_type is not EvidenceType.TIME_SERIES_OBSERVATION:
        return False
    if item.observation_date is None:
        return False

    earlier = session.execute(
        select(ExternalEvent).where(
            ExternalEvent.source_id == source_id,
            ExternalEvent.series_key == item.series_key,
            ExternalEvent.observation_date == item.observation_date,
            ExternalEvent.id != new_id,
            ExternalEvent.superseded_by_id.is_(None),
        )
    ).scalars().all()

    if not earlier:
        return False

    for row in earlier:
        # The old row keeps its value and its known_at. Only the pointer is
        # added, so "what did we believe in July" remains answerable.
        row.superseded_by_id = new_id
    return True


def source_row_ids(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(ExternalSource.source_key, ExternalSource.id).where(
            ExternalSource.source_key.isnot(None)
        )
    ).all()
    return {key: row_id for key, row_id in rows}


def latest_observation(
    session: Session, series_key: str, *, as_of: datetime
) -> ExternalEvent | None:
    """Newest observation of a series that was knowable at ``as_of``.

    The ``known_at <= as_of`` filter is the whole point of this function: it is
    what a backtest calls, and it is the same call production makes with
    ``as_of = now``. One code path, so the two cannot diverge.
    """
    return session.execute(
        select(ExternalEvent)
        .where(
            ExternalEvent.series_key == series_key,
            ExternalEvent.known_at <= as_of,
            ExternalEvent.numeric_value.isnot(None),
        )
        .order_by(
            ExternalEvent.observation_date.desc().nullslast(),
            ExternalEvent.known_at.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()


def observation_history(
    session: Session, series_key: str, *, as_of: datetime, limit: int = 24
) -> list[ExternalEvent]:
    """Distinct observation periods for a series, newest first, known at as_of.

    One row per period — the value we believed at ``as_of``, which for a revised
    figure is the latest revision that had been published by then, not the
    figure as it stands today.
    """
    rows = session.execute(
        select(ExternalEvent)
        .where(
            ExternalEvent.series_key == series_key,
            ExternalEvent.known_at <= as_of,
            ExternalEvent.numeric_value.isnot(None),
            ExternalEvent.observation_date.isnot(None),
        )
        .order_by(
            ExternalEvent.observation_date.desc(),
            ExternalEvent.known_at.desc(),
        )
        .limit(limit * 4)
    ).scalars().all()

    seen: set[datetime] = set()
    history: list[ExternalEvent] = []
    for row in rows:
        if row.observation_date in seen:
            continue
        seen.add(row.observation_date)
        history.append(row)
        if len(history) >= limit:
            break
    return history


def evidence_counts_by_source(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(ExternalSource.source_key, func.count(ExternalEvent.id))
        .join(ExternalEvent, ExternalEvent.source_id == ExternalSource.id)
        .group_by(ExternalSource.source_key)
    ).all()
    return {key: int(count) for key, count in rows if key}
