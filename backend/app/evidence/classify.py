"""Deep market classification.

Phase 1's classifier answers "which category"; this answers the three further
questions that determine whether an independent probability is even possible:

* **subcategory** — which evidence sources apply. "Will the Fed cut in September"
  and "Will CPI exceed 3%" are both MACRO but need different feeds.
* **event_type** — the shape of the question, which determines how a number
  becomes a probability. A THRESHOLD question ("BTC above $100k") maps a
  distribution onto a level; a SELECTION question ("who wins the nomination")
  does not.
* **resolution_mechanism** — who decides. An official statistic resolves
  cleanly; media consensus does not, and that difference is most of resolution
  risk.

Deterministic. Patterns are ordered and the first match wins, so the same
question always classifies the same way and a misclassification can be traced to
one named rule. The optional LLM layer may *refine* this, never replace it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.enums import (
    EventType,
    MarketCategory,
    MarketSubcategory,
    ResolutionMechanism,
)

# Threshold extraction: the number and direction a question turns on.
_THRESHOLD_PATTERNS = (
    re.compile(r"\b(above|over|exceed|greater than|higher than|more than|at least|reach)\b", re.I),
    re.compile(r"\b(below|under|less than|lower than|fewer than|at most|drop to)\b", re.I),
)
_MONEY = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kmb])?\b", re.I)
_PERCENT = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%")
_BPS = re.compile(r"([0-9]+)\s*(?:bps|basis points)", re.I)

# (subcategory, pattern, event type, resolution mechanism). Order matters:
# more specific rules first, because the first match wins.
_RULES: tuple[tuple[MarketSubcategory, re.Pattern[str], EventType, ResolutionMechanism], ...] = (
    (
        MarketSubcategory.FED_RATES,
        re.compile(r"\b(fed|fomc|federal reserve)\b.{0,40}\b(cut|hike|raise|lower|hold|unchanged|rate|bps|basis points)\b|\brate (cut|hike)\b", re.I),
        EventType.SCHEDULED_ANNOUNCEMENT,
        ResolutionMechanism.OFFICIAL_ANNOUNCEMENT,
    ),
    (
        MarketSubcategory.FED_PERSONNEL,
        re.compile(r"\b(fed chair|federal reserve chair|powell|fed governor|nominated to the fed)\b", re.I),
        EventType.SELECTION,
        ResolutionMechanism.OFFICIAL_ANNOUNCEMENT,
    ),
    (
        MarketSubcategory.INFLATION,
        re.compile(r"\b(cpi|inflation|consumer price|pce|core cpi|deflation)\b", re.I),
        EventType.THRESHOLD,
        ResolutionMechanism.OFFICIAL_STATISTIC,
    ),
    (
        MarketSubcategory.EMPLOYMENT,
        re.compile(r"\b(unemployment|jobless|payrolls|jobs report|nonfarm|initial claims|labor force)\b", re.I),
        EventType.THRESHOLD,
        ResolutionMechanism.OFFICIAL_STATISTIC,
    ),
    (
        MarketSubcategory.RECESSION,
        re.compile(r"\brecession\b|\bnber\b|\btwo consecutive quarters\b", re.I),
        EventType.OCCURRENCE,
        ResolutionMechanism.OFFICIAL_STATISTIC,
    ),
    (
        MarketSubcategory.GDP_GROWTH,
        re.compile(r"\bgdp\b|\bgross domestic product\b|\beconomic growth\b", re.I),
        EventType.THRESHOLD,
        ResolutionMechanism.OFFICIAL_STATISTIC,
    ),
    (
        MarketSubcategory.TREASURY_YIELDS,
        re.compile(r"\b(treasury|10-?year yield|2-?year yield|yield curve|bond yield)\b", re.I),
        EventType.THRESHOLD,
        ResolutionMechanism.MARKET_PRICE,
    ),
    (
        MarketSubcategory.CRYPTO_REGULATION,
        re.compile(r"\b(sec|cftc|regulat\w+|etf approval|approve.{0,20}etf)\b.{0,40}\b(bitcoin|btc|ethereum|eth|crypto)\b", re.I),
        EventType.OCCURRENCE,
        ResolutionMechanism.REGULATORY_FILING,
    ),
    (
        MarketSubcategory.CRYPTO_PRICE,
        re.compile(r"\b(bitcoin|btc|ethereum|eth|solana|sol|xrp|ripple|dogecoin|doge)\b", re.I),
        EventType.THRESHOLD,
        ResolutionMechanism.MARKET_PRICE,
    ),
    (
        MarketSubcategory.CRYPTO_PROTOCOL,
        re.compile(r"\b(hard fork|mainnet|halving|upgrade|merge)\b", re.I),
        EventType.OCCURRENCE,
        ResolutionMechanism.OFFICIAL_ANNOUNCEMENT,
    ),
    (
        MarketSubcategory.US_PRESIDENTIAL,
        re.compile(r"\b(presidential election|win the presidency|president in \d{4}|next president)\b", re.I),
        EventType.SELECTION,
        ResolutionMechanism.ELECTION_AUTHORITY,
    ),
    (
        MarketSubcategory.US_PRIMARY,
        re.compile(r"\b(primary|nominee|nomination|caucus)\b", re.I),
        EventType.SELECTION,
        ResolutionMechanism.ELECTION_AUTHORITY,
    ),
    (
        MarketSubcategory.US_CONGRESSIONAL,
        re.compile(r"\b(senate|house of representatives|midterm|congressional)\b.{0,30}\b(win|control|majority|seat)\b", re.I),
        EventType.SELECTION,
        ResolutionMechanism.ELECTION_AUTHORITY,
    ),
    (
        MarketSubcategory.APPOINTMENT,
        re.compile(r"\b(appointed|nominated|confirmed as|next (ceo|prime minister|chair|director|secretary))\b", re.I),
        EventType.SELECTION,
        ResolutionMechanism.OFFICIAL_ANNOUNCEMENT,
    ),
    (
        MarketSubcategory.LEGISLATION,
        re.compile(r"\b(bill|act|legislation|pass the (house|senate)|signed into law|shutdown)\b", re.I),
        EventType.OCCURRENCE,
        ResolutionMechanism.OFFICIAL_ANNOUNCEMENT,
    ),
    (
        MarketSubcategory.INTERNATIONAL_ELECTION,
        re.compile(r"\b(election|elected)\b", re.I),
        EventType.SELECTION,
        ResolutionMechanism.ELECTION_AUTHORITY,
    ),
    (
        MarketSubcategory.CORPORATE_EARNINGS,
        re.compile(r"\b(earnings|revenue|eps|quarterly results|beat estimates)\b", re.I),
        EventType.THRESHOLD,
        ResolutionMechanism.REGULATORY_FILING,
    ),
    (
        MarketSubcategory.CORPORATE_EVENT,
        re.compile(r"\b(ipo|acquisition|merger|bankrupt|layoffs|ceo|delisted)\b", re.I),
        EventType.OCCURRENCE,
        ResolutionMechanism.REGULATORY_FILING,
    ),
)

# Subcategory -> the category it implies, used to correct a shallow tag-based
# category when the question text is more specific than the venue's tags.
_IMPLIED_CATEGORY: dict[MarketSubcategory, MarketCategory] = {
    MarketSubcategory.FED_RATES: MarketCategory.FEDERAL_RESERVE,
    MarketSubcategory.FED_PERSONNEL: MarketCategory.FEDERAL_RESERVE,
    MarketSubcategory.INFLATION: MarketCategory.MACROECONOMICS,
    MarketSubcategory.EMPLOYMENT: MarketCategory.MACROECONOMICS,
    MarketSubcategory.GDP_GROWTH: MarketCategory.MACROECONOMICS,
    MarketSubcategory.RECESSION: MarketCategory.MACROECONOMICS,
    MarketSubcategory.TREASURY_YIELDS: MarketCategory.MACROECONOMICS,
    MarketSubcategory.CRYPTO_PRICE: MarketCategory.CRYPTO,
    MarketSubcategory.CRYPTO_PROTOCOL: MarketCategory.CRYPTO,
    MarketSubcategory.CRYPTO_REGULATION: MarketCategory.CRYPTO,
    MarketSubcategory.US_PRESIDENTIAL: MarketCategory.ELECTIONS,
    MarketSubcategory.US_CONGRESSIONAL: MarketCategory.ELECTIONS,
    MarketSubcategory.US_PRIMARY: MarketCategory.ELECTIONS,
    MarketSubcategory.INTERNATIONAL_ELECTION: MarketCategory.ELECTIONS,
    MarketSubcategory.CORPORATE_EARNINGS: MarketCategory.BUSINESS,
    MarketSubcategory.CORPORATE_EVENT: MarketCategory.BUSINESS,
}

# Assets the crypto model can actually price, keyed by the tokens that name them.
_CRYPTO_ASSETS = {
    "btc": "BTC", "bitcoin": "BTC",
    "eth": "ETH", "ethereum": "ETH", "ether": "ETH",
    "sol": "SOL", "solana": "SOL",
    "xrp": "XRP", "ripple": "XRP",
    "doge": "DOGE", "dogecoin": "DOGE",
}


@dataclass
class DeepClassification:
    subcategory: MarketSubcategory
    event_type: EventType
    resolution_mechanism: ResolutionMechanism
    implied_category: MarketCategory | None
    confidence: float
    matched_rule: str
    subject_tags: tuple[str, ...] = ()
    threshold_value: float | None = None
    threshold_direction: str | None = None
    """"above" or "below" — which side of the threshold resolves YES."""
    asset: str | None = None

    def as_detail(self) -> dict:
        return {
            "subcategory": self.subcategory.value,
            "event_type": self.event_type.value,
            "resolution_mechanism": self.resolution_mechanism.value,
            "implied_category": self.implied_category.value if self.implied_category else None,
            "confidence": round(self.confidence, 3),
            "matched_rule": self.matched_rule,
            "subject_tags": list(self.subject_tags),
            "threshold_value": self.threshold_value,
            "threshold_direction": self.threshold_direction,
            "asset": self.asset,
        }


def classify_deep(
    *,
    question: str | None,
    description: str | None = None,
    category: MarketCategory | None = None,
) -> DeepClassification:
    """Classify a market's subcategory, shape and resolution mechanism.

    Reads the question primarily. Description is consulted only for threshold
    extraction, because descriptions are long and matching subcategory rules
    against them produces false positives.
    """
    text = (question or "").strip()
    if not text:
        return DeepClassification(
            subcategory=MarketSubcategory.UNCLASSIFIED,
            event_type=EventType.UNKNOWN,
            resolution_mechanism=ResolutionMechanism.UNKNOWN,
            implied_category=None,
            confidence=0.0,
            matched_rule="no question text",
        )

    for subcategory, pattern, event_type, mechanism in _RULES:
        match = pattern.search(text)
        if not match:
            continue

        threshold, direction = _extract_threshold(text, description)
        asset = _extract_asset(text) if subcategory is MarketSubcategory.CRYPTO_PRICE else None

        return DeepClassification(
            subcategory=subcategory,
            event_type=event_type,
            resolution_mechanism=mechanism,
            implied_category=_IMPLIED_CATEGORY.get(subcategory),
            # A specific rule matching the question is strong evidence, but it
            # is still a keyword match rather than comprehension.
            confidence=0.80,
            matched_rule=f"{subcategory.value}:{match.group(0)[:40].lower()}",
            subject_tags=_subject_tags(text, subcategory, asset),
            threshold_value=threshold,
            threshold_direction=direction,
            asset=asset,
        )

    return DeepClassification(
        subcategory=MarketSubcategory.UNCLASSIFIED,
        event_type=EventType.UNKNOWN,
        resolution_mechanism=(
            ResolutionMechanism.MEDIA_CONSENSUS
            if category in (MarketCategory.GEOPOLITICS, MarketCategory.ENTERTAINMENT)
            else ResolutionMechanism.UNKNOWN
        ),
        implied_category=None,
        confidence=0.15,
        matched_rule="no rule matched",
        subject_tags=(),
    )


# ---------------------------------------------------------------------------
def _extract_threshold(question: str, description: str | None) -> tuple[float | None, str | None]:
    """Pull the number and direction a threshold question turns on.

    Returns (None, None) when there is no unambiguous threshold. Guessing here
    would be worse than not knowing: a wrong threshold produces a confidently
    wrong probability.
    """
    direction: str | None = None
    if _THRESHOLD_PATTERNS[0].search(question):
        direction = "above"
    elif _THRESHOLD_PATTERNS[1].search(question):
        direction = "below"

    for source in (question, description or ""):
        if not source:
            continue

        money = _MONEY.search(source)
        if money:
            try:
                value = float(money.group(1).replace(",", ""))
            except ValueError:
                value = None
            if value is not None:
                suffix = (money.group(2) or "").lower()
                multiplier = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(suffix, 1)
                return value * multiplier, direction

        bps = _BPS.search(source)
        if bps:
            try:
                return float(bps.group(1)) / 100.0, direction
            except ValueError:
                pass

        percent = _PERCENT.search(source)
        if percent:
            try:
                return float(percent.group(1)), direction
            except ValueError:
                pass

    return None, direction


def _extract_asset(question: str) -> str | None:
    lowered = question.lower()
    for token, symbol in _CRYPTO_ASSETS.items():
        if re.search(rf"\b{re.escape(token)}\b", lowered):
            return symbol
    return None


def _subject_tags(
    question: str, subcategory: MarketSubcategory, asset: str | None
) -> tuple[str, ...]:
    """Tokens the evidence matcher uses to find relevant items."""
    tags: set[str] = {subcategory.value.lower()}
    if asset:
        tags.add(asset.lower())

    lowered = question.lower()
    for keyword in (
        "cpi", "inflation", "unemployment", "payrolls", "jobs report", "gdp",
        "recession", "fed", "fomc", "federal reserve", "rate cut", "rate hike",
        "treasury", "yield", "bitcoin", "ethereum", "election", "nominee",
        "primary", "senate", "earnings", "sec", "candidate",
    ):
        if keyword in lowered:
            tags.add(keyword)

    return tuple(sorted(tags))
