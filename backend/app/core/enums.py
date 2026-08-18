"""Domain enumerations.

These are the vocabulary of the system. They are deliberately explicit — there
is a distinct value for "we do not know" everywhere it is possible not to know,
because collapsing unknown into a default is how fabricated data gets born.
"""

from __future__ import annotations

import enum


class MarketStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"


class MarketCategory(str, enum.Enum):
    POLITICS = "POLITICS"
    ELECTIONS = "ELECTIONS"
    MACROECONOMICS = "MACROECONOMICS"
    FEDERAL_RESERVE = "FEDERAL_RESERVE"
    CRYPTO = "CRYPTO"
    SPORTS = "SPORTS"
    TECHNOLOGY = "TECHNOLOGY"
    BUSINESS = "BUSINESS"
    GEOPOLITICS = "GEOPOLITICS"
    ENTERTAINMENT = "ENTERTAINMENT"
    OTHER = "OTHER"


class ModelabilityStatus(str, enum.Enum):
    TRADEABLE = "TRADEABLE"
    WATCHLIST = "WATCHLIST"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    UNMODELABLE = "UNMODELABLE"
    RESOLUTION_RISK = "RESOLUTION_RISK"


class Recommendation(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    WATCH = "WATCH"
    NO_TRADE = "NO_TRADE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class Side(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class RiskStatus(str, enum.Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    BLOCKED_BY_KILL_SWITCH = "BLOCKED_BY_KILL_SWITCH"
    NOT_EVALUATED = "NOT_EVALUATED"


class ResolutionRisk(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class SourceTier(int, enum.Enum):
    TIER_1_AUTHORITATIVE = 1
    TIER_2_REPUTABLE_NEWS = 2
    TIER_3_SPECIALIST = 3
    TIER_4_SOCIAL = 4


class SourceType(str, enum.Enum):
    OFFICIAL_GOVERNMENT = "official_government"
    OFFICIAL_COMPANY = "official_company"
    MARKET_DATA = "market_data"
    ON_CHAIN = "on_chain"
    NEWS = "news"
    RESEARCH = "research"
    POLLING = "polling"
    SOCIAL_MEDIA = "social_media"
    MODEL_OUTPUT = "model_output"


class VerificationStatus(str, enum.Enum):
    """How much weight a datum has earned. Nothing starts as CONFIRMED_FACT."""

    CONFIRMED_FACT = "CONFIRMED_FACT"
    REPORTED_INFORMATION = "REPORTED_INFORMATION"
    UNCONFIRMED_CLAIM = "UNCONFIRMED_CLAIM"
    ANALYST_OPINION = "ANALYST_OPINION"
    UNVERIFIED = "UNVERIFIED"


class ComponentHealth(str, enum.Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    FAILED = "FAILED"
    DISABLED = "DISABLED"
    UNKNOWN = "UNKNOWN"


class SystemComponent(str, enum.Enum):
    DATA_FEED = "DATA_FEED"
    MARKET_DISCOVERY = "MARKET_DISCOVERY"
    PROBABILITY_ENGINE = "PROBABILITY_ENGINE"
    EDGE_ENGINE = "EDGE_ENGINE"
    RISK_ENGINE = "RISK_ENGINE"
    PAPER_ENGINE = "PAPER_ENGINE"
    RESOLUTION_ENGINE = "RESOLUTION_ENGINE"
    METRICS_ENGINE = "METRICS_ENGINE"
    DATABASE = "DATABASE"
    FRONTEND = "FRONTEND"
    WORKERS = "WORKERS"


class KillSwitch(str, enum.Enum):
    GLOBAL = "GLOBAL_KILL_SWITCH"
    DATA = "DATA_KILL_SWITCH"
    MODEL = "MODEL_KILL_SWITCH"
    RISK = "RISK_KILL_SWITCH"
    CONNECTIVITY = "CONNECTIVITY_KILL_SWITCH"


class ExecutionVenue(str, enum.Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class OrderState(str, enum.Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ResolutionOutcome(str, enum.Enum):
    YES = "YES"
    NO = "NO"
    INVALID = "INVALID"
    CANCELLED = "CANCELLED"
    AMBIGUOUS = "AMBIGUOUS"
    UNKNOWN = "UNKNOWN"
