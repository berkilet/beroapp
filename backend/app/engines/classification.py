"""Market classification.

Deterministic and explainable: a market's category comes from its venue tags
first, and only falls back to keyword matching on the question when tags are
absent or uninformative. There is no model here and no LLM — classification is
a routing decision, and a routing decision that cannot be explained is a
liability.

The output carries a confidence so that downstream code can distinguish "this
is tagged Fed" from "this mentions the word rate".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.enums import MarketCategory

# Tag slug -> category. Tags are the venue's own taxonomy, so a tag match is
# high-confidence. Order within the tuple does not matter; priority is applied
# by CATEGORY_PRIORITY below.
_TAG_MAP: dict[str, MarketCategory] = {
    "fed": MarketCategory.FEDERAL_RESERVE,
    "fed-rates": MarketCategory.FEDERAL_RESERVE,
    "federal-reserve": MarketCategory.FEDERAL_RESERVE,
    "fomc": MarketCategory.FEDERAL_RESERVE,
    "interest-rates": MarketCategory.FEDERAL_RESERVE,
    "elections": MarketCategory.ELECTIONS,
    "us-election": MarketCategory.ELECTIONS,
    "world-elections": MarketCategory.ELECTIONS,
    "primaries": MarketCategory.ELECTIONS,
    "politics": MarketCategory.POLITICS,
    "us-politics": MarketCategory.POLITICS,
    "trump": MarketCategory.POLITICS,
    "congress": MarketCategory.POLITICS,
    "economy": MarketCategory.MACROECONOMICS,
    "inflation": MarketCategory.MACROECONOMICS,
    "economic-policy": MarketCategory.MACROECONOMICS,
    "cpi": MarketCategory.MACROECONOMICS,
    "gdp": MarketCategory.MACROECONOMICS,
    "jobs": MarketCategory.MACROECONOMICS,
    "recession": MarketCategory.MACROECONOMICS,
    "crypto": MarketCategory.CRYPTO,
    "bitcoin": MarketCategory.CRYPTO,
    "ethereum": MarketCategory.CRYPTO,
    "solana": MarketCategory.CRYPTO,
    "crypto-prices": MarketCategory.CRYPTO,
    "sports": MarketCategory.SPORTS,
    "nfl": MarketCategory.SPORTS,
    "nba": MarketCategory.SPORTS,
    "mlb": MarketCategory.SPORTS,
    "soccer": MarketCategory.SPORTS,
    "epl": MarketCategory.SPORTS,
    "tennis": MarketCategory.SPORTS,
    "football": MarketCategory.SPORTS,
    "games": MarketCategory.SPORTS,
    "tech": MarketCategory.TECHNOLOGY,
    "ai": MarketCategory.TECHNOLOGY,
    "artificial-intelligence": MarketCategory.TECHNOLOGY,
    "space": MarketCategory.TECHNOLOGY,
    "business": MarketCategory.BUSINESS,
    "earnings": MarketCategory.BUSINESS,
    "companies": MarketCategory.BUSINESS,
    "stocks": MarketCategory.BUSINESS,
    "ipo": MarketCategory.BUSINESS,
    "geopolitics": MarketCategory.GEOPOLITICS,
    "middle-east": MarketCategory.GEOPOLITICS,
    "ukraine": MarketCategory.GEOPOLITICS,
    "russia": MarketCategory.GEOPOLITICS,
    "china": MarketCategory.GEOPOLITICS,
    "israel": MarketCategory.GEOPOLITICS,
    "war": MarketCategory.GEOPOLITICS,
    "nato": MarketCategory.GEOPOLITICS,
    "entertainment": MarketCategory.ENTERTAINMENT,
    "movies": MarketCategory.ENTERTAINMENT,
    "music": MarketCategory.ENTERTAINMENT,
    "awards": MarketCategory.ENTERTAINMENT,
    "oscars": MarketCategory.ENTERTAINMENT,
    "pop-culture": MarketCategory.ENTERTAINMENT,
    "celebrities": MarketCategory.ENTERTAINMENT,
}

# When several tags match, the most *specific* category wins. FEDERAL_RESERVE
# beats MACROECONOMICS beats POLITICS, because a market tagged both "fed" and
# "economy" is a Fed market and should route to the Fed model.
CATEGORY_PRIORITY: list[MarketCategory] = [
    MarketCategory.FEDERAL_RESERVE,
    MarketCategory.ELECTIONS,
    MarketCategory.CRYPTO,
    MarketCategory.SPORTS,
    MarketCategory.MACROECONOMICS,
    MarketCategory.GEOPOLITICS,
    MarketCategory.BUSINESS,
    MarketCategory.TECHNOLOGY,
    MarketCategory.ENTERTAINMENT,
    MarketCategory.POLITICS,
    MarketCategory.OTHER,
]

# Fallback keyword patterns, applied to the question text only when tags gave
# us nothing. Deliberately conservative: a weak signal should produce OTHER
# rather than a confident wrong answer.
_KEYWORD_PATTERNS: list[tuple[MarketCategory, re.Pattern[str]]] = [
    (MarketCategory.FEDERAL_RESERVE, re.compile(r"\b(fed|fomc|federal reserve|rate (cut|hike)|basis points?|bps)\b", re.I)),
    (MarketCategory.ELECTIONS, re.compile(r"\b(elect(ion|ed)|primary|nominee|ballot|caucus|win the (presidency|senate|house))\b", re.I)),
    (MarketCategory.CRYPTO, re.compile(r"\b(bitcoin|btc|ethereum|eth|solana|sol|crypto|stablecoin|token|blockchain)\b", re.I)),
    (MarketCategory.MACROECONOMICS, re.compile(r"\b(cpi|inflation|unemployment|gdp|recession|jobs report|payrolls|pce)\b", re.I)),
    (MarketCategory.SPORTS, re.compile(r"\b(vs\.?|beat|defeat|championship|super bowl|world cup|playoffs|nba|nfl|mlb|nhl)\b", re.I)),
    (MarketCategory.GEOPOLITICS, re.compile(r"\b(ceasefire|invade|invasion|sanctions?|treaty|war|nato|military strike)\b", re.I)),
    (MarketCategory.BUSINESS, re.compile(r"\b(earnings|revenue|ipo|acquisition|merger|ceo|bankrupt)\b", re.I)),
    (MarketCategory.TECHNOLOGY, re.compile(r"\b(gpt|llm|\bai\b|openai|anthropic|launch|rocket|satellite)\b", re.I)),
    (MarketCategory.ENTERTAINMENT, re.compile(r"\b(oscar|grammy|emmy|box office|album|billboard|rotten tomatoes)\b", re.I)),
    (MarketCategory.POLITICS, re.compile(r"\b(president|senate|congress|impeach|cabinet|resign|prime minister|parliament)\b", re.I)),
]


@dataclass(frozen=True)
class Classification:
    category: MarketCategory
    confidence: float
    matched_on: str
    evidence: tuple[str, ...]


def classify(
    *,
    question: str | None,
    tag_slugs: list[str] | None = None,
    tag_labels: list[str] | None = None,
) -> Classification:
    """Assign a category, with a confidence and an explanation.

    Confidence semantics:
      0.90 — matched one or more venue tags
      0.55 — matched a keyword in the question text
      0.20 — nothing matched; category is OTHER and downstream code should treat
             it as unrouted rather than as a genuine "other" market
    """
    candidates: dict[MarketCategory, list[str]] = {}

    for slug in _normalise_tags(tag_slugs, tag_labels):
        category = _TAG_MAP.get(slug)
        if category is not None:
            candidates.setdefault(category, []).append(f"tag:{slug}")

    if candidates:
        chosen = _highest_priority(candidates)
        return Classification(
            category=chosen,
            confidence=0.90,
            matched_on="tags",
            evidence=tuple(candidates[chosen]),
        )

    if question:
        keyword_hits: dict[MarketCategory, list[str]] = {}
        for category, pattern in _KEYWORD_PATTERNS:
            match = pattern.search(question)
            if match:
                keyword_hits.setdefault(category, []).append(f"keyword:{match.group(0).lower()}")
        if keyword_hits:
            chosen = _highest_priority(keyword_hits)
            return Classification(
                category=chosen,
                confidence=0.55,
                matched_on="question_keywords",
                evidence=tuple(keyword_hits[chosen]),
            )

    return Classification(
        category=MarketCategory.OTHER, confidence=0.20, matched_on="none", evidence=()
    )


def _normalise_tags(slugs: list[str] | None, labels: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in list(slugs or []) + list(labels or []):
        if not value:
            continue
        out.append(re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-"))
    return out


def _highest_priority(candidates: dict[MarketCategory, list[str]]) -> MarketCategory:
    for category in CATEGORY_PRIORITY:
        if category in candidates:
            return category
    return MarketCategory.OTHER
