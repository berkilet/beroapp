"""Market-to-evidence matching.

Decides which evidence bears on which market, and how much. This is where the
spec's rule "do not dump every article into every model" is enforced: a Fed
market gets Treasury, BLS and the FOMC calendar, and nothing else.

Matching is deterministic and every link records the named rule that produced
it, so a wrong association can be traced rather than guessed at. Relevance is a
score in [0,1] used later as a feature weight, not a filter — a weak link is
recorded weakly rather than discarded, because the difference between "no
evidence" and "weak evidence" is exactly what the confidence layer needs.

The link's own ``known_at`` is the later of the evidence's and the market's
first observation. Evidence published before a market existed is still relevant
to it — July's CPI bears on a market created in August — but the *link* only
becomes usable once both sides exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.enums import MarketSubcategory
from app.db.models import ExternalEvent, Market, MarketEvidenceLink

# Subcategory -> series that inform it. The core routing table: it is what
# stops a crypto feed reaching an inflation market.
SERIES_FOR_SUBCATEGORY: dict[MarketSubcategory, tuple[str, ...]] = {
    MarketSubcategory.FED_RATES: (
        "UST_YIELD_1M", "UST_YIELD_3M", "UST_YIELD_6M", "UST_YIELD_1Y", "UST_YIELD_2Y",
        "FOMC_MEETING", "CPI_URBAN_ALL", "CPI_CORE", "UNEMPLOYMENT_RATE",
    ),
    MarketSubcategory.FED_PERSONNEL: ("FOMC_MEETING",),
    MarketSubcategory.INFLATION: (
        "CPI_URBAN_ALL", "CPI_CORE", "UST_YIELD_2Y", "UST_YIELD_10Y",
    ),
    MarketSubcategory.EMPLOYMENT: (
        "UNEMPLOYMENT_RATE", "NONFARM_PAYROLLS",
    ),
    MarketSubcategory.RECESSION: (
        "UNEMPLOYMENT_RATE", "NONFARM_PAYROLLS", "UST_YIELD_2Y", "UST_YIELD_10Y",
        "UST_YIELD_3M",
    ),
    MarketSubcategory.GDP_GROWTH: ("UNEMPLOYMENT_RATE", "NONFARM_PAYROLLS"),
    MarketSubcategory.TREASURY_YIELDS: (
        "UST_YIELD_3M", "UST_YIELD_2Y", "UST_YIELD_10Y", "UST_YIELD_30Y",
    ),
    MarketSubcategory.US_PRESIDENTIAL: ("FEC_CANDIDATE",),
    MarketSubcategory.US_PRIMARY: ("FEC_CANDIDATE",),
    MarketSubcategory.US_CONGRESSIONAL: ("FEC_CANDIDATE",),
}

# Relevance by how directly the series speaks to the question.
_PRIMARY_RELEVANCE = 0.90
_SUPPORTING_RELEVANCE = 0.55
_ASSET_MATCH_RELEVANCE = 0.95
_WEAK_RELEVANCE = 0.30

# Series that are the *subject* of a subcategory rather than context for it.
_PRIMARY_SERIES: dict[MarketSubcategory, frozenset[str]] = {
    MarketSubcategory.INFLATION: frozenset({"CPI_URBAN_ALL", "CPI_CORE"}),
    MarketSubcategory.EMPLOYMENT: frozenset({"UNEMPLOYMENT_RATE", "NONFARM_PAYROLLS"}),
    MarketSubcategory.FED_RATES: frozenset(
        {"FOMC_MEETING", "UST_YIELD_1M", "UST_YIELD_3M", "UST_YIELD_6M"}
    ),
    MarketSubcategory.TREASURY_YIELDS: frozenset(
        {"UST_YIELD_3M", "UST_YIELD_2Y", "UST_YIELD_10Y", "UST_YIELD_30Y"}
    ),
}


@dataclass
class MatchResult:
    evidence_id: int
    relevance: float
    reason: str
    detail: dict


def relevant_series(
    subcategory: MarketSubcategory | None,
    *,
    asset: str | None = None,
    ticker: str | None = None,
) -> tuple[str, ...]:
    """Series keys that could inform this market. Empty means unmodelable here."""
    keys: list[str] = []

    if subcategory is not None:
        keys.extend(SERIES_FOR_SUBCATEGORY.get(subcategory, ()))

    if asset:
        keys.extend([f"CRYPTO_SPOT_{asset}_USD", f"CRYPTO_VOL_{asset}_USD"])
    if ticker:
        keys.extend(
            [
                f"SEC_FILING_{ticker}_8-K",
                f"SEC_FILING_{ticker}_10-Q",
                f"SEC_FILING_{ticker}_10-K",
            ]
        )

    # Deduplicate while preserving order — the first series listed is the most
    # directly relevant, and downstream code relies on that ordering.
    seen: set[str] = set()
    ordered: list[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return tuple(ordered)


def score_match(
    *,
    series_key: str,
    subcategory: MarketSubcategory | None,
    asset: str | None,
    subject_tags: frozenset[str],
    evidence_tags: frozenset[str],
) -> tuple[float, str] | None:
    """Relevance of one evidence series to one market, with the reason.

    Returns None when there is no defensible link. Returning a small non-zero
    score for everything would be worse: it would make "unrelated" and "weakly
    related" indistinguishable.
    """
    # An asset-specific series matching the market's own asset is the strongest
    # possible link — this is BTC evidence for a BTC market.
    if asset and f"_{asset}_" in series_key:
        return _ASSET_MATCH_RELEVANCE, f"asset_match:{asset}"

    if subcategory is not None:
        mapped = SERIES_FOR_SUBCATEGORY.get(subcategory, ())
        if series_key in mapped:
            primary = _PRIMARY_SERIES.get(subcategory, frozenset())
            if series_key in primary:
                return _PRIMARY_RELEVANCE, f"primary_series:{subcategory.value}"
            return _SUPPORTING_RELEVANCE, f"supporting_series:{subcategory.value}"

    # Fall back to tag overlap, which catches genuine matches the routing table
    # has not been extended for yet.
    overlap = subject_tags & evidence_tags
    if overlap:
        return _WEAK_RELEVANCE, f"tag_overlap:{','.join(sorted(overlap)[:3])}"

    return None


def link_evidence_for_market(
    session: Session,
    market: Market,
    *,
    subcategory: MarketSubcategory | None,
    asset: str | None,
    ticker: str | None,
    subject_tags: tuple[str, ...],
    as_of: datetime,
    max_links: int = 40,
) -> list[MatchResult]:
    """Create links between a market and the evidence that bears on it.

    Idempotent: re-running produces no duplicates, so a crashed worker can
    safely repeat a cycle.
    """
    series = relevant_series(subcategory, asset=asset, ticker=ticker)
    if not series:
        return []

    # Only evidence knowable at as_of. In production as_of is now; in a backtest
    # it is historical, and the same query serves both.
    candidates = session.execute(
        select(ExternalEvent)
        .where(
            ExternalEvent.series_key.in_(series),
            ExternalEvent.known_at <= as_of,
        )
        .order_by(ExternalEvent.known_at.desc())
        .limit(max_links * 4)
    ).scalars().all()

    market_tags = frozenset(t.lower() for t in subject_tags)
    matches: list[MatchResult] = []
    seen_series: set[str] = set()

    for evidence in candidates:
        payload = evidence.payload or {}
        evidence_tags = frozenset(str(t).lower() for t in payload.get("subject_tags", []))

        scored = score_match(
            series_key=evidence.series_key or "",
            subcategory=subcategory,
            asset=asset,
            subject_tags=market_tags,
            evidence_tags=evidence_tags,
        )
        if scored is None:
            continue
        relevance, reason = scored

        # One link per (series, observation period). Linking every historical
        # revision of the same figure would multiply weight by revision count.
        dedupe_key = f"{evidence.series_key}:{evidence.observation_date}"
        if dedupe_key in seen_series:
            continue
        seen_series.add(dedupe_key)

        matches.append(
            MatchResult(
                evidence_id=evidence.id,
                relevance=relevance,
                reason=reason,
                detail={
                    "series_key": evidence.series_key,
                    "source_tier": evidence.source_tier,
                    "observation_date": (
                        evidence.observation_date.isoformat()
                        if evidence.observation_date
                        else None
                    ),
                },
            )
        )
        if len(matches) >= max_links:
            break

    for match in matches:
        evidence_known_at = session.get(ExternalEvent, match.evidence_id).known_at
        session.execute(
            pg_insert(MarketEvidenceLink)
            .values(
                market_id=market.id,
                evidence_id=match.evidence_id,
                relevance=match.relevance,
                match_reason=match.reason,
                match_detail=match.detail,
                # The link is usable only once both sides exist.
                known_at=max(evidence_known_at, market.first_seen_at),
                created_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=["market_id", "evidence_id"])
        )

    return matches


def linked_evidence(
    session: Session, market_id: int, *, as_of: datetime, min_relevance: float = 0.0
) -> list[tuple[MarketEvidenceLink, ExternalEvent]]:
    """Evidence linked to a market and knowable at ``as_of``, best links first."""
    rows = session.execute(
        select(MarketEvidenceLink, ExternalEvent)
        .join(ExternalEvent, ExternalEvent.id == MarketEvidenceLink.evidence_id)
        .where(
            MarketEvidenceLink.market_id == market_id,
            MarketEvidenceLink.known_at <= as_of,
            ExternalEvent.known_at <= as_of,
            MarketEvidenceLink.relevance >= min_relevance,
        )
        .order_by(
            MarketEvidenceLink.relevance.desc(),
            ExternalEvent.observation_date.desc().nullslast(),
        )
    ).all()
    return [(link, event) for link, event in rows]


def evidence_source_count(
    session: Session, market_id: int, *, as_of: datetime, min_relevance: float = 0.5
) -> int:
    """Distinct sources backing a market. Feeds the signal-strength gate, which
    requires corroboration from more than one source before it will emit."""
    rows = linked_evidence(session, market_id, as_of=as_of, min_relevance=min_relevance)
    return len({event.source_id for _, event in rows})
