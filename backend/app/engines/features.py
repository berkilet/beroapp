"""Feature engineering.

Turns market microstructure plus linked evidence into a numeric vector, with a
timestamp on every single feature.

The per-feature timestamp is the point. A vector's `known_at` says when the
whole thing was assembled, but a CPI figure inside it may be six weeks old while
the spread beside it is six seconds old. Without per-feature ages, a model
trained on "current inflation" is silently trained on "inflation as of whenever
we last managed to fetch it", and nobody notices until the fetch breaks.

Three rules, all enforced here rather than trusted to callers:

* **Nothing enters without a known_at at or before `as_of`.** The same code path
  runs in production (`as_of = now`) and in backtest (`as_of` historical), so
  look-ahead cannot appear in one and not the other.
* **A missing feature is named, not defaulted.** Substituting zero for an
  unavailable CPI reading would produce a confident number from no information.
  Missing features are listed, and a model that needs one refuses to run.
* **Every feature traces to the evidence rows that produced it.**
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.enums import MarketCategory, MarketSubcategory
from app.db.models import Market, MarketSnapshot
from app.engines.liquidity import LiquidityProfile
from app.evidence.conflicts import authoritative_value
from app.evidence.matching import linked_evidence
from app.evidence.store import observation_history

FEATURE_SET_VERSION = "fs-1.0.0"

# Features every market gets, from microstructure alone. Always available when
# there is a book, so they are never in `missing`.
MARKET_FEATURES = (
    "market_midpoint",
    "executable_price",
    "spread",
    "spread_pct",
    "book_imbalance",
    "total_depth_usd",
    "liquidity_num",
    "volume_num",
    "volume_24hr",
    "hours_to_resolution",
    "log_hours_to_resolution",
    "snapshot_count",
    "price_change_1h",
    "price_change_24h",
    "price_volatility_24h",
)

# Evidence-derived features by subcategory. A model for a subcategory declares
# which of these it requires; anything required and missing blocks the model.
EVIDENCE_FEATURES: dict[MarketSubcategory, tuple[str, ...]] = {
    MarketSubcategory.FED_RATES: (
        "ust_3m", "ust_2y", "ust_10y", "curve_3m_10y", "curve_2y_10y",
        "ust_3m_change_30d", "days_to_next_fomc", "cpi_yoy", "unemployment_rate",
    ),
    MarketSubcategory.INFLATION: (
        "cpi_yoy", "cpi_mom", "core_cpi_yoy", "ust_2y", "ust_10y", "curve_2y_10y",
    ),
    MarketSubcategory.EMPLOYMENT: (
        "unemployment_rate", "unemployment_change_3m", "payrolls_change_1m",
    ),
    MarketSubcategory.RECESSION: (
        "curve_3m_10y", "curve_2y_10y", "unemployment_rate", "unemployment_change_3m",
    ),
    MarketSubcategory.TREASURY_YIELDS: (
        "ust_3m", "ust_2y", "ust_10y", "ust_30y", "curve_2y_10y",
    ),
    MarketSubcategory.CRYPTO_PRICE: (
        "spot_price", "realised_volatility", "distance_to_threshold",
        "normalised_distance", "threshold_z_score",
    ),
}


@dataclass
class FeatureVector:
    market_id: int
    token_id: str
    category: MarketCategory
    subcategory: MarketSubcategory | None
    known_at: datetime
    features: dict[str, float] = field(default_factory=dict)
    timestamps: dict[str, str] = field(default_factory=dict)
    evidence_ids: list[int] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    version: str = FEATURE_SET_VERSION

    def set(
        self,
        name: str,
        value: float | None,
        *,
        known_at: datetime | None = None,
        evidence_id: int | None = None,
    ) -> None:
        """Record a feature, or record that it is missing.

        The two are different facts and this is the only place they are
        distinguished, so getting it right here fixes it everywhere.
        """
        if value is None or not math.isfinite(float(value)):
            if name not in self.missing:
                self.missing.append(name)
            return
        self.features[name] = float(value)
        self.timestamps[name] = (known_at or self.known_at).isoformat()
        if evidence_id is not None and evidence_id not in self.evidence_ids:
            self.evidence_ids.append(evidence_id)

    def has_all(self, required: tuple[str, ...]) -> bool:
        return all(name in self.features for name in required)

    def missing_from(self, required: tuple[str, ...]) -> list[str]:
        return [name for name in required if name not in self.features]

    def oldest_feature_age_s(self, now: datetime | None = None) -> float | None:
        """Age of the stalest input. A vector is only as fresh as its oldest part."""
        if not self.timestamps:
            return None
        now = now or self.known_at
        ages = []
        for stamp in self.timestamps.values():
            try:
                parsed = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            ages.append((now - parsed).total_seconds())
        return max(ages) if ages else None

    def evidence_feature_count(self) -> int:
        """How many features came from outside Polymarket.

        The honest measure of whether an estimate is independent: zero means the
        model is looking only at the price it is trying to beat.
        """
        market_names = set(MARKET_FEATURES)
        return sum(1 for name in self.features if name not in market_names)


class FeatureBuilder:
    """Assembles feature vectors. Stateless apart from settings."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def build(
        self,
        session: Session,
        *,
        market: Market,
        token_id: str,
        profile: LiquidityProfile,
        executable_price: float | None,
        snapshot_count: int,
        as_of: datetime,
        subcategory: MarketSubcategory | None,
        asset: str | None = None,
        threshold: float | None = None,
        threshold_direction: str | None = None,
    ) -> FeatureVector:
        vector = FeatureVector(
            market_id=market.id,
            token_id=token_id,
            category=MarketCategory(market.category),
            subcategory=subcategory,
            known_at=as_of,
        )

        self._market_features(
            session, vector, market, token_id, profile, executable_price, snapshot_count, as_of
        )

        if subcategory in (
            MarketSubcategory.FED_RATES,
            MarketSubcategory.INFLATION,
            MarketSubcategory.EMPLOYMENT,
            MarketSubcategory.RECESSION,
            MarketSubcategory.TREASURY_YIELDS,
        ):
            self._macro_features(session, vector, as_of)
        elif subcategory is MarketSubcategory.CRYPTO_PRICE:
            self._crypto_features(
                session, vector, as_of, asset, threshold, threshold_direction, market
            )

        self._evidence_meta(session, vector, market.id, as_of)
        return vector

    # ------------------------------------------------------------------
    def _market_features(
        self,
        session: Session,
        vector: FeatureVector,
        market: Market,
        token_id: str,
        profile: LiquidityProfile,
        executable_price: float | None,
        snapshot_count: int,
        as_of: datetime,
    ) -> None:
        vector.set("market_midpoint", profile.midpoint)
        vector.set("executable_price", executable_price)
        vector.set("spread", profile.spread)
        vector.set("spread_pct", profile.spread_pct)
        vector.set("book_imbalance", profile.imbalance)
        vector.set("total_depth_usd", profile.total_depth_usd)
        vector.set("liquidity_num", market.liquidity_num)
        vector.set("volume_num", market.volume_num)
        vector.set("volume_24hr", market.volume_24hr)
        vector.set("snapshot_count", float(snapshot_count))

        if market.end_date is not None:
            hours = (market.end_date - as_of).total_seconds() / 3600.0
            vector.set("hours_to_resolution", hours)
            # Log scale because the difference between 1h and 24h matters far
            # more than between 200 days and 223 days.
            vector.set("log_hours_to_resolution", math.log1p(max(0.0, hours)))
        else:
            vector.missing.extend(["hours_to_resolution", "log_hours_to_resolution"])

        self._price_history_features(session, vector, token_id, as_of)

    def _price_history_features(
        self, session: Session, vector: FeatureVector, token_id: str, as_of: datetime
    ) -> None:
        """Recent price action, from snapshots knowable at as_of."""
        from sqlalchemy import select

        rows = session.execute(
            select(MarketSnapshot.midpoint, MarketSnapshot.known_at)
            .where(
                MarketSnapshot.token_id == token_id,
                MarketSnapshot.known_at <= as_of,
                MarketSnapshot.known_at >= as_of - timedelta(hours=48),
                MarketSnapshot.midpoint.isnot(None),
            )
            .order_by(MarketSnapshot.known_at.desc())
            .limit(500)
        ).all()

        if len(rows) < 2:
            vector.missing.extend(["price_change_1h", "price_change_24h", "price_volatility_24h"])
            return

        current = float(rows[0][0])

        for label, hours in (("price_change_1h", 1), ("price_change_24h", 24)):
            cutoff = as_of - timedelta(hours=hours)
            past = next((float(m) for m, k in rows if k <= cutoff), None)
            vector.set(label, current - past if past is not None else None)

        values = [float(m) for m, _ in rows]
        if len(values) >= 5:
            mean = sum(values) / len(values)
            variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
            vector.set("price_volatility_24h", math.sqrt(variance))
        else:
            vector.missing.append("price_volatility_24h")

    # ------------------------------------------------------------------
    def _macro_features(self, session: Session, vector: FeatureVector, as_of: datetime) -> None:
        """Yields, curve slopes, inflation and labour, all conflict-resolved."""
        yields: dict[str, float] = {}

        for name, series in (
            ("ust_3m", "UST_YIELD_3M"),
            ("ust_2y", "UST_YIELD_2Y"),
            ("ust_10y", "UST_YIELD_10Y"),
            ("ust_30y", "UST_YIELD_30Y"),
        ):
            value, event, _ = authoritative_value(session, series, as_of=as_of)
            vector.set(
                name, value,
                known_at=event.known_at if event else None,
                evidence_id=event.id if event else None,
            )
            if value is not None:
                yields[name] = value

        # Curve slopes: inversion is the single most-watched recession signal,
        # and it is a derived feature rather than an ingested one.
        if "ust_3m" in yields and "ust_10y" in yields:
            vector.set("curve_3m_10y", yields["ust_10y"] - yields["ust_3m"])
        else:
            vector.missing.append("curve_3m_10y")
        if "ust_2y" in yields and "ust_10y" in yields:
            vector.set("curve_2y_10y", yields["ust_10y"] - yields["ust_2y"])
        else:
            vector.missing.append("curve_2y_10y")

        # Thirty-day change in the short end: the market repricing policy.
        history = observation_history(session, "UST_YIELD_3M", as_of=as_of, limit=40)
        if len(history) >= 2 and "ust_3m" in yields:
            cutoff = as_of - timedelta(days=30)
            past = next((h for h in history if h.known_at <= cutoff), None)
            vector.set(
                "ust_3m_change_30d",
                yields["ust_3m"] - float(past.numeric_value) if past and past.numeric_value else None,
            )
        else:
            vector.missing.append("ust_3m_change_30d")

        self._inflation_features(session, vector, as_of)
        self._labour_features(session, vector, as_of)
        self._fomc_features(session, vector, as_of)

    def _inflation_features(
        self, session: Session, vector: FeatureVector, as_of: datetime
    ) -> None:
        """Year-over-year and month-over-month CPI, derived from the index.

        BLS publishes an index level, not a rate. Computing the rate here rather
        than ingesting one keeps the arithmetic visible and the provenance
        intact — both index readings that produced the rate are recorded.
        """
        for name, series in (("cpi_yoy", "CPI_URBAN_ALL"), ("core_cpi_yoy", "CPI_CORE")):
            history = observation_history(session, series, as_of=as_of, limit=15)
            if len(history) < 13:
                vector.missing.append(name)
                if name == "cpi_yoy":
                    vector.missing.append("cpi_mom")
                continue

            latest = history[0]
            year_ago = history[12]
            if latest.numeric_value and year_ago.numeric_value:
                yoy = (latest.numeric_value / year_ago.numeric_value - 1.0) * 100.0
                vector.set(name, yoy, known_at=latest.known_at, evidence_id=latest.id)
            else:
                vector.missing.append(name)

            if name == "cpi_yoy":
                previous = history[1]
                if latest.numeric_value and previous.numeric_value:
                    mom = (latest.numeric_value / previous.numeric_value - 1.0) * 100.0
                    vector.set("cpi_mom", mom, known_at=latest.known_at, evidence_id=latest.id)
                else:
                    vector.missing.append("cpi_mom")

    def _labour_features(self, session: Session, vector: FeatureVector, as_of: datetime) -> None:
        value, event, _ = authoritative_value(session, "UNEMPLOYMENT_RATE", as_of=as_of)
        vector.set(
            "unemployment_rate", value,
            known_at=event.known_at if event else None,
            evidence_id=event.id if event else None,
        )

        history = observation_history(session, "UNEMPLOYMENT_RATE", as_of=as_of, limit=6)
        if len(history) >= 4 and history[0].numeric_value and history[3].numeric_value:
            vector.set(
                "unemployment_change_3m",
                history[0].numeric_value - history[3].numeric_value,
                known_at=history[0].known_at,
            )
        else:
            vector.missing.append("unemployment_change_3m")

        payrolls = observation_history(session, "NONFARM_PAYROLLS", as_of=as_of, limit=3)
        if len(payrolls) >= 2 and payrolls[0].numeric_value and payrolls[1].numeric_value:
            vector.set(
                "payrolls_change_1m",
                payrolls[0].numeric_value - payrolls[1].numeric_value,
                known_at=payrolls[0].known_at,
            )
        else:
            vector.missing.append("payrolls_change_1m")

    def _fomc_features(self, session: Session, vector: FeatureVector, as_of: datetime) -> None:
        """Days to the next scheduled meeting.

        Structural rather than predictive, and essential: a market asking about
        a September decision is unanswerable without knowing when September's
        meeting is.
        """
        from sqlalchemy import select

        from app.db.models import ExternalEvent

        next_meeting = session.execute(
            select(ExternalEvent)
            .where(
                ExternalEvent.series_key == "FOMC_MEETING",
                ExternalEvent.known_at <= as_of,
                ExternalEvent.observation_date > as_of,
            )
            .order_by(ExternalEvent.observation_date.asc())
            .limit(1)
        ).scalar_one_or_none()

        if next_meeting is None or next_meeting.observation_date is None:
            vector.missing.append("days_to_next_fomc")
            return

        days = (next_meeting.observation_date - as_of).total_seconds() / 86_400.0
        vector.set(
            "days_to_next_fomc", days,
            known_at=next_meeting.known_at, evidence_id=next_meeting.id,
        )

    # ------------------------------------------------------------------
    def _crypto_features(
        self,
        session: Session,
        vector: FeatureVector,
        as_of: datetime,
        asset: str | None,
        threshold: float | None,
        direction: str | None,
        market: Market,
    ) -> None:
        """Spot, realised volatility, and the market's distance to its threshold.

        `threshold_z_score` is the useful one: how many standard deviations of
        expected movement separate today's price from the level the question
        turns on, scaled by the time remaining. That is the quantity a
        lognormal-diffusion view of price actually depends on.
        """
        if asset is None:
            vector.missing.extend(EVIDENCE_FEATURES[MarketSubcategory.CRYPTO_PRICE])
            return

        spot, spot_event, _ = authoritative_value(
            session, f"CRYPTO_SPOT_{asset}_USD", as_of=as_of
        )
        vector.set(
            "spot_price", spot,
            known_at=spot_event.known_at if spot_event else None,
            evidence_id=spot_event.id if spot_event else None,
        )

        vol_90, vol90_event, _ = authoritative_value(
            session, f"CRYPTO_VOL_{asset}_USD", as_of=as_of
        )
        vol_30, vol30_event, _ = authoritative_value(
            session, f"CRYPTO_VOL30_{asset}_USD", as_of=as_of
        )
        vector.set(
            "realised_volatility_90d", vol_90,
            known_at=vol90_event.known_at if vol90_event else None,
            evidence_id=vol90_event.id if vol90_event else None,
        )
        vector.set(
            "realised_volatility_30d", vol_30,
            known_at=vol30_event.known_at if vol30_event else None,
            evidence_id=vol30_event.id if vol30_event else None,
        )

        # Horizon-matched selection. Volatility clusters, so for a question
        # resolving within a fortnight the recent regime is a better estimate of
        # the coming fortnight than a quarter of history is.
        hours_ahead = vector.features.get("hours_to_resolution")
        prefer_short = hours_ahead is not None and hours_ahead <= 21 * 24
        volatility = (vol_30 if prefer_short and vol_30 is not None else None) or vol_90
        chosen_event = (
            vol30_event if (prefer_short and vol_30 is not None) else vol90_event
        )
        vector.set(
            "realised_volatility", volatility,
            known_at=chosen_event.known_at if chosen_event else None,
            evidence_id=chosen_event.id if chosen_event else None,
        )
        if volatility is not None:
            vector.set("volatility_window_days", 30.0 if (prefer_short and vol_30 is not None) else 90.0)

        if spot is None or threshold is None or threshold <= 0 or spot <= 0:
            vector.missing.extend(
                ["distance_to_threshold", "normalised_distance", "threshold_z_score"]
            )
            return

        vector.set("distance_to_threshold", threshold - spot)
        vector.set("normalised_distance", (threshold - spot) / spot)

        hours = vector.features.get("hours_to_resolution")
        if volatility is None or hours is None or hours <= 0:
            vector.missing.append("threshold_z_score")
            return

        years = hours / (24.0 * 365.0)
        sigma = volatility * math.sqrt(max(years, 1e-9))
        if sigma <= 0:
            vector.missing.append("threshold_z_score")
            return

        # log(threshold/spot) / sigma_over_horizon. Positive means the threshold
        # is above spot and needs an upward move to be reached.
        vector.set("threshold_z_score", math.log(threshold / spot) / sigma)

    # ------------------------------------------------------------------
    def _evidence_meta(
        self, session: Session, vector: FeatureVector, market_id: int, as_of: datetime
    ) -> None:
        """Features about the evidence itself, which the confidence layer needs."""
        links = linked_evidence(session, market_id, as_of=as_of, min_relevance=0.3)

        vector.set("evidence_item_count", float(len(links)))
        vector.set("evidence_source_count", float(len({e.source_id for _, e in links})))

        if links:
            best_tier = min(e.source_tier for _, e in links)
            vector.set("best_evidence_tier", float(best_tier))
            vector.set(
                "mean_evidence_relevance",
                sum(link.relevance for link, _ in links) / len(links),
            )
            newest = max(e.known_at for _, e in links)
            vector.set(
                "evidence_age_hours", (as_of - newest).total_seconds() / 3600.0
            )
        else:
            vector.missing.extend(
                ["best_evidence_tier", "mean_evidence_relevance", "evidence_age_hours"]
            )


def required_features(subcategory: MarketSubcategory | None) -> tuple[str, ...]:
    """Features a category model needs before it may produce an estimate."""
    if subcategory is None:
        return ()
    return EVIDENCE_FEATURES.get(subcategory, ())
