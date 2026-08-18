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


# ---------------------------------------------------------------------------
# Phase 1.5: evidence, classification depth, modelability tiers
# ---------------------------------------------------------------------------


class MarketSubcategory(str, enum.Enum):
    """Finer routing than category. Determines which evidence sources apply.

    A market's category says which model family; its subcategory says which
    sources to actually query. "Will the Fed cut in September" and "Will CPI
    exceed 3%" are both MACRO but need different feeds.
    """

    FED_RATES = "FED_RATES"
    FED_PERSONNEL = "FED_PERSONNEL"
    INFLATION = "INFLATION"
    EMPLOYMENT = "EMPLOYMENT"
    GDP_GROWTH = "GDP_GROWTH"
    RECESSION = "RECESSION"
    TREASURY_YIELDS = "TREASURY_YIELDS"

    CRYPTO_PRICE = "CRYPTO_PRICE"
    CRYPTO_PROTOCOL = "CRYPTO_PROTOCOL"
    CRYPTO_REGULATION = "CRYPTO_REGULATION"

    US_PRESIDENTIAL = "US_PRESIDENTIAL"
    US_CONGRESSIONAL = "US_CONGRESSIONAL"
    US_PRIMARY = "US_PRIMARY"
    INTERNATIONAL_ELECTION = "INTERNATIONAL_ELECTION"
    APPOINTMENT = "APPOINTMENT"
    LEGISLATION = "LEGISLATION"

    CORPORATE_EARNINGS = "CORPORATE_EARNINGS"
    CORPORATE_EVENT = "CORPORATE_EVENT"

    UNCLASSIFIED = "UNCLASSIFIED"


class EventType(str, enum.Enum):
    """The shape of the question, which determines how evidence maps to it."""

    THRESHOLD = "THRESHOLD"
    """A published number crosses a level (CPI > 3%, BTC > $100k)."""

    SCHEDULED_ANNOUNCEMENT = "SCHEDULED_ANNOUNCEMENT"
    """A known body announces on a known date (FOMC decision, CPI release)."""

    SELECTION = "SELECTION"
    """One of N candidates is chosen (nominee, appointee)."""

    OCCURRENCE = "OCCURRENCE"
    """An unscheduled event happens or does not (resignation, invasion)."""

    UNKNOWN = "UNKNOWN"


class ResolutionMechanism(str, enum.Enum):
    """Who or what determines the outcome. Drives resolution risk."""

    OFFICIAL_STATISTIC = "OFFICIAL_STATISTIC"
    OFFICIAL_ANNOUNCEMENT = "OFFICIAL_ANNOUNCEMENT"
    MARKET_PRICE = "MARKET_PRICE"
    ELECTION_AUTHORITY = "ELECTION_AUTHORITY"
    REGULATORY_FILING = "REGULATORY_FILING"
    MEDIA_CONSENSUS = "MEDIA_CONSENSUS"
    DISCRETIONARY = "DISCRETIONARY"
    UNKNOWN = "UNKNOWN"


class ModelabilityTier(str, enum.Enum):
    """Coarse tier required by the Phase 1.5 spec.

    Sits alongside the existing ModelabilityStatus rather than replacing it:
    the status drives Phase 1 filtering, the tier drives which probability
    model may run.
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNMODELABLE = "UNMODELABLE"


class EvidenceType(str, enum.Enum):
    """What kind of fact an evidence row carries."""

    TIME_SERIES_OBSERVATION = "TIME_SERIES_OBSERVATION"
    SCHEDULED_EVENT = "SCHEDULED_EVENT"
    FILING = "FILING"
    ANNOUNCEMENT = "ANNOUNCEMENT"
    MARKET_QUOTE = "MARKET_QUOTE"
    POLL = "POLL"
    NEWS_ITEM = "NEWS_ITEM"
    REGISTRY_RECORD = "REGISTRY_RECORD"


class SignalStrength(str, enum.Enum):
    """Graduated signal states required by the Phase 1.5 spec.

    Sits between the raw edge computation and the existing Recommendation
    enum, so Phase 1 consumers keep working unchanged.
    """

    NONE = "NONE"
    WATCH = "WATCH"
    CANDIDATE = "CANDIDATE"
    SIGNAL = "SIGNAL"


class ConflictResolution(str, enum.Enum):
    """Why one evidence item won over another."""

    HIGHER_TIER = "HIGHER_TIER"
    MORE_RECENT = "MORE_RECENT"
    BETTER_VERIFIED = "BETTER_VERIFIED"
    MORE_RELIABLE_SOURCE = "MORE_RELIABLE_SOURCE"
    UNRESOLVED = "UNRESOLVED"
