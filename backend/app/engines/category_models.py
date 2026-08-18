"""Category-specific independent probability models.

The Phase 1 baseline anchors to the market and departs only for microstructure
reasons. These models are the first components that can disagree with the market
on the strength of *outside information* — and the whole point of Phase 1.5.

Each model is interpretable, states the assumption it rests on, and refuses to
produce a number when its required features are missing. That refusal is the
most important behaviour in the file: a model that substitutes a default for an
unavailable CPI reading returns a confident number derived from nothing.

Two models are implemented today, and only two, because only two subcategories
have genuine keyless evidence behind them:

* **Crypto threshold** — a lognormal diffusion view of price. Well-founded, and
  the one place this platform has a real analytical edge over a naive reading of
  the price, because it uses realised volatility the market may be mispricing.
* **Macro threshold** — inflation and unemployment against a published level,
  using the observed series and its recent trend.

Everything else falls back to the Phase 1 baseline and says so. Fed-rate markets
in particular are deliberately *not* modelled: doing it properly needs
fed-funds-futures-implied probabilities, and CME's data is not freely available.
Reading a rate decision off the Treasury curve would be a guess dressed as a
model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.core.config import Settings, get_settings
from app.core.enums import MarketSubcategory
from app.engines.features import FeatureVector
from app.engines.probability import validate_probability
from app.evidence.question_shape import QuestionShape, ShapeResult


class _AlreadyBreached(Exception):
    """The condition is already settled by observation, so no forecast applies."""

MODEL_VERSION_PREFIX = "v0.2.0"


@dataclass
class CategoryEstimate:
    """An independent probability, or an explicit refusal to produce one."""

    probability: float | None
    uncertainty: float
    model_id: str
    model_version: str
    available: bool
    reason: str
    features_used: dict[str, float] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    missing_features: list[str] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        return self.available and self.probability is not None


def _unavailable(model_id: str, reason: str, missing: list[str] | None = None) -> CategoryEstimate:
    return CategoryEstimate(
        probability=None,
        uncertainty=1.0,
        model_id=model_id,
        model_version=f"{MODEL_VERSION_PREFIX}-{model_id}",
        available=False,
        reason=reason,
        missing_features=missing or [],
    )


def _normal_cdf(x: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class CryptoThresholdModel:
    """Probability that a crypto price satisfies a stated condition by expiry.

    Models log-price as a driftless Brownian motion with volatility estimated
    from the last 90 daily closes. Driftless is deliberate: estimating crypto
    drift from 90 days produces a number dominated by whichever way the sample
    happened to run, and a drift estimate that wrong is worse than none.

    **Four shapes, four different formulas.** This is the part that matters. An
    earlier version applied the terminal formula to every question and reported
    a 27-point edge on "Will Bitcoin dip to $62,000?" — which asks whether the
    path ever *touches* 62,000, not where it ends. The formulas below are the
    exact first-passage results for Brownian motion with drift, not
    approximations:

      terminal        P(S_T > K)                 = Phi(d)
      barrier above   P(max S_t >= K)            = Phi(a) + exp(2*mu*b/sigma^2)*Phi(c)
      barrier below   P(min S_t <= K)            = Phi(a) + exp(2*mu*b/sigma^2)*Phi(c)
      range           P(K1 < S_T < K2)           = Phi(d1) - Phi(d2)

    A barrier probability is strictly larger than the corresponding terminal
    probability, and near the money roughly double it. Getting that wrong is the
    difference between a real signal and an invented one.

    Where the model declines, and each is enforced in code:
      * a question whose shape cannot be determined;
      * horizons beyond ~180 days, where a driftless walk is a poor description;
      * sub-hour horizons, where microstructure dominates diffusion;
      * a threshold already breached at the time of the estimate;
      * an asset whose volatility could not be measured.
    """

    model_id = "crypto_threshold"
    MAX_HORIZON_HOURS = 180 * 24
    MIN_HORIZON_HOURS = 1.0

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def model_version(self) -> str:
        return f"{MODEL_VERSION_PREFIX}-{self.model_id}"

    def estimate(self, vector: FeatureVector, *, shape: ShapeResult) -> CategoryEstimate:
        if not shape.is_modelable:
            return _unavailable(
                self.model_id, f"question shape not modelable: {shape.reason}"
            )

        for name in ("spot_price", "realised_volatility"):
            if name not in vector.features:
                return _unavailable(self.model_id, f"{name} unavailable", [name])

        spot = vector.features["spot_price"]
        volatility = vector.features["realised_volatility"]
        hours = vector.features.get("hours_to_resolution")

        if hours is None:
            return _unavailable(self.model_id, "no resolution horizon", ["hours_to_resolution"])
        if hours < self.MIN_HORIZON_HOURS:
            return _unavailable(
                self.model_id,
                f"horizon {hours:.2f}h is below {self.MIN_HORIZON_HOURS}h; at this scale "
                "microstructure dominates diffusion and the model does not apply",
            )
        if hours > self.MAX_HORIZON_HOURS:
            return _unavailable(
                self.model_id,
                f"horizon {hours / 24:.0f}d exceeds {self.MAX_HORIZON_HOURS / 24:.0f}d; "
                "a driftless walk is not a defensible description over that span",
            )
        if volatility <= 0 or spot <= 0:
            return _unavailable(self.model_id, "non-positive spot or volatility")

        years = hours / (24.0 * 365.0)
        sigma = volatility * math.sqrt(years)
        if sigma <= 0:
            return _unavailable(self.model_id, "degenerate volatility over the horizon")

        # Ito drift in log space for a driftless price process.
        mu_t = -0.5 * volatility * volatility * years

        try:
            probability, detail = self._probability_for_shape(
                shape, spot=spot, sigma=sigma, mu_t=mu_t
            )
        except _AlreadyBreached as exc:
            return _unavailable(self.model_id, str(exc))

        probability = validate_probability(
            min(0.999, max(0.001, probability)),
            model_version=self.model_version,
            field_name="crypto threshold probability",
        )

        return CategoryEstimate(
            probability=probability,
            uncertainty=self._uncertainty(sigma, hours, vector, shape),
            model_id=self.model_id,
            model_version=self.model_version,
            available=True,
            reason=f"{shape.shape.value.lower()} probability under driftless lognormal diffusion",
            features_used={
                "spot_price": spot,
                "realised_volatility": volatility,
                "horizon_years": years,
                "sigma_over_horizon": sigma,
                "shape": shape.shape.value,
                "lower": shape.lower,
                "upper": shape.upper,
                **detail,
            },
            assumptions=[
                "log price follows a driftless random walk over the horizon",
                "volatility over the horizon equals realised volatility of the last 90 daily closes",
                f"the question's shape is {shape.shape.value} ({shape.reason})",
                "the threshold(s) parsed from the question text are correct",
            ],
        )

    # ------------------------------------------------------------------
    def _probability_for_shape(
        self, shape: ShapeResult, *, spot: float, sigma: float, mu_t: float
    ) -> tuple[float, dict]:
        variance = sigma * sigma

        if shape.shape is QuestionShape.TERMINAL:
            if shape.lower is not None:
                # P(S_T > K)
                d = (math.log(spot / shape.lower) + mu_t) / sigma
                return _normal_cdf(d), {"d": d, "direction": "above"}
            # P(S_T < K)
            d = (math.log(shape.upper / spot) - mu_t) / sigma
            return _normal_cdf(d), {"d": d, "direction": "below"}

        if shape.shape is QuestionShape.RANGE:
            # P(K1 < S_T < K2) = Phi(upper) - Phi(lower)
            d_upper = (math.log(shape.upper / spot) - mu_t) / sigma
            d_lower = (math.log(shape.lower / spot) - mu_t) / sigma
            return max(0.0, _normal_cdf(d_upper) - _normal_cdf(d_lower)), {
                "d_upper": d_upper,
                "d_lower": d_lower,
                "direction": "between",
            }

        if shape.shape is QuestionShape.BARRIER_ABOVE:
            barrier = shape.lower
            if barrier <= spot:
                # Already at or above the level. The question is settled by
                # observation, not by a model, and pretending otherwise would
                # produce a fabricated probability.
                raise _AlreadyBreached(
                    f"spot {spot:.2f} is already at or above the {barrier:.2f} barrier; "
                    "the outcome is an observation, not a forecast"
                )
            b = math.log(barrier / spot)  # > 0
            # P(max_t X_t >= b) for X_t = mu*t + sigma*W_t
            first = _normal_cdf((-b + mu_t) / sigma)
            second = math.exp(2.0 * mu_t * b / variance) * _normal_cdf((-b - mu_t) / sigma)
            return min(1.0, first + second), {"b": b, "direction": "touch_above"}

        if shape.shape is QuestionShape.BARRIER_BELOW:
            barrier = shape.lower if shape.lower is not None else shape.upper
            if barrier >= spot:
                raise _AlreadyBreached(
                    f"spot {spot:.2f} is already at or below the {barrier:.2f} barrier; "
                    "the outcome is an observation, not a forecast"
                )
            b = math.log(barrier / spot)  # < 0
            # P(min_t X_t <= b)
            first = _normal_cdf((b - mu_t) / sigma)
            second = math.exp(2.0 * mu_t * b / variance) * _normal_cdf((b + mu_t) / sigma)
            return min(1.0, first + second), {"b": b, "direction": "touch_below"}

        raise _AlreadyBreached(f"unhandled shape {shape.shape.value}")

    def _uncertainty(
        self, sigma: float, hours: float, vector: FeatureVector, shape: ShapeResult
    ) -> float:
        """How much to distrust this estimate."""
        uncertainty = 0.35

        days = hours / 24.0
        uncertainty += min(0.30, days / 180.0 * 0.30)
        if sigma > 1.0:
            uncertainty += 0.15

        # Barrier probabilities are more sensitive to the volatility estimate
        # than terminal ones, because the whole path matters rather than one
        # endpoint. That extra model risk belongs in the uncertainty.
        if shape.shape in (QuestionShape.BARRIER_ABOVE, QuestionShape.BARRIER_BELOW):
            uncertainty += 0.10

        age_hours = vector.features.get("evidence_age_hours")
        if age_hours is not None and age_hours > 6:
            uncertainty += min(0.15, age_hours / 48.0 * 0.15)

        if vector.features.get("evidence_source_count", 0) >= 2:
            uncertainty -= 0.05

        return max(0.10, min(0.95, uncertainty))


class MacroThresholdModel:
    """P(a published statistic is above/below a level at the next release).

    Applies to inflation and unemployment markets where the question names a
    level. The estimate combines the latest published value with its recent
    trend, and treats the residual as normal with a category-specific standard
    deviation taken from how much these series actually move month to month.

    Honest about its limits: monthly statistics move slowly and are heavily
    forecast by professionals, so this model rarely disagrees with a liquid
    market by much. It exists so that when the market *does* drift away from
    what the published series implies, that is visible and measurable.
    """

    model_id = "macro_threshold"

    # Typical one-month standard deviation of each series, from published
    # history. Used as the residual scale; deliberately generous, because
    # understating it would manufacture confident edges.
    MONTHLY_SIGMA = {
        "cpi_yoy": 0.25,
        "core_cpi_yoy": 0.20,
        "unemployment_rate": 0.15,
    }

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def model_version(self) -> str:
        return f"{MODEL_VERSION_PREFIX}-{self.model_id}"

    def estimate(
        self,
        vector: FeatureVector,
        *,
        subcategory: MarketSubcategory,
        threshold: float | None,
        direction: str | None,
    ) -> CategoryEstimate:
        feature_name = {
            MarketSubcategory.INFLATION: "cpi_yoy",
            MarketSubcategory.EMPLOYMENT: "unemployment_rate",
        }.get(subcategory)

        if feature_name is None:
            return _unavailable(self.model_id, f"no macro series maps to {subcategory.value}")
        if threshold is None:
            return _unavailable(
                self.model_id, "no threshold could be parsed from the question"
            )
        if feature_name not in vector.features:
            return _unavailable(
                self.model_id, f"{feature_name} is unavailable", [feature_name]
            )

        current = vector.features[feature_name]
        hours = vector.features.get("hours_to_resolution")
        if hours is None or hours <= 0:
            return _unavailable(self.model_id, "no usable resolution horizon")

        months = max(0.5, hours / (24.0 * 30.0))

        # Trend, where we have it. Momentum in monthly macro series is real but
        # weak, so it is damped hard rather than extrapolated.
        trend_per_month = 0.0
        trend_feature = {
            "cpi_yoy": "cpi_mom",
            "unemployment_rate": "unemployment_change_3m",
        }.get(feature_name)
        if trend_feature == "unemployment_change_3m" and trend_feature in vector.features:
            trend_per_month = vector.features[trend_feature] / 3.0
        elif trend_feature == "cpi_mom" and trend_feature in vector.features:
            trend_per_month = 0.0  # monthly CPI does not extrapolate to the annual rate

        expected = current + trend_per_month * months * 0.5

        base_sigma = self.MONTHLY_SIGMA.get(feature_name, 0.25)
        sigma = base_sigma * math.sqrt(months)
        if sigma <= 0:
            return _unavailable(self.model_id, "degenerate residual scale")

        z = (threshold - expected) / sigma
        probability_above = 1.0 - _normal_cdf(z)
        probability = probability_above if direction != "below" else 1.0 - probability_above
        probability = validate_probability(
            min(0.99, max(0.01, probability)),
            model_version=self.model_version,
            field_name="macro threshold probability",
        )

        return CategoryEstimate(
            probability=probability,
            uncertainty=self._uncertainty(months, vector),
            model_id=self.model_id,
            model_version=self.model_version,
            available=True,
            reason=f"normal residual around the published {feature_name} and its damped trend",
            features_used={
                "current_value": current,
                "expected_at_resolution": expected,
                "threshold": threshold,
                "months_ahead": months,
                "sigma": sigma,
                "z": z,
            },
            assumptions=[
                f"{feature_name} at resolution is normal around its current value",
                f"one-month standard deviation of {base_sigma} scales with sqrt(months)",
                "trend is damped by half and never extrapolated beyond the horizon",
                "the threshold parsed from the question text is correct",
            ],
        )

    def _uncertainty(self, months: float, vector: FeatureVector) -> float:
        uncertainty = 0.45
        uncertainty += min(0.30, months / 12.0 * 0.30)

        age_hours = vector.features.get("evidence_age_hours")
        if age_hours is not None and age_hours > 24 * 45:
            # A statistic older than ~45 days means we have missed a release.
            uncertainty += 0.20

        if vector.features.get("evidence_item_count", 0) >= 5:
            uncertainty -= 0.05

        return max(0.15, min(0.95, uncertainty))


class CategoryModelRouter:
    """Routes a market to its category model, or reports that none applies."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.crypto = CryptoThresholdModel(self.settings)
        self.macro = MacroThresholdModel(self.settings)

    def estimate(
        self,
        vector: FeatureVector,
        *,
        subcategory: MarketSubcategory | None,
        threshold: float | None,
        direction: str | None,
        shape: ShapeResult | None = None,
    ) -> CategoryEstimate:
        if subcategory is None:
            return _unavailable("none", "market has no recognised subcategory")

        # A model must have genuine outside information, not just a price.
        evidence_features = vector.evidence_feature_count()
        if evidence_features < self.settings.min_evidence_items_for_model:
            return _unavailable(
                "none",
                f"only {evidence_features} evidence-derived features available; "
                f"{self.settings.min_evidence_items_for_model} required before a model "
                "may claim an independent estimate",
            )

        if subcategory is MarketSubcategory.CRYPTO_PRICE:
            if shape is None:
                return _unavailable("none", "no question shape was supplied for a price market")
            return self.crypto.estimate(vector, shape=shape)

        if subcategory in (MarketSubcategory.INFLATION, MarketSubcategory.EMPLOYMENT):
            return self.macro.estimate(
                vector, subcategory=subcategory, threshold=threshold, direction=direction
            )

        return _unavailable(
            "none",
            f"no category model is implemented for {subcategory.value}; "
            "the market-anchored baseline applies",
        )

    @property
    def implemented_subcategories(self) -> tuple[MarketSubcategory, ...]:
        return (
            MarketSubcategory.CRYPTO_PRICE,
            MarketSubcategory.INFLATION,
            MarketSubcategory.EMPLOYMENT,
        )
