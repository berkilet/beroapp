"""Question shape detection for threshold markets.

A price threshold market can ask several *mathematically different* questions,
and answering the wrong one produces a confident, wrong probability. This module
exists because an earlier version of this platform did exactly that: it read
"Will Bitcoin dip to $62,000?" as "will the terminal price exceed $62,000", got
0.80 against a market at 0.26, and reported a 27-point edge that was entirely an
artefact of asking the wrong question.

The four shapes, and why they differ:

* **TERMINAL** — "will X be above K on DATE". P(S_T > K). The textbook case.
* **BARRIER_ABOVE** — "will X reach/hit K". P(max over the period ≥ K). Strictly
  larger than the terminal probability, because the path can touch K and fall
  back. Roughly double it near the money.
* **BARRIER_BELOW** — "will X dip/fall to K". P(min over the period ≤ K).
* **RANGE** — "will X be between K1 and K2". Needs both bounds; one bound alone
  is meaningless.

Anything that does not clearly match one of these is `UNKNOWN`, and the model
refuses rather than guessing. "Bitcoin Up or Down" — a comparison against an
unstated opening price — is the common example, and it is correctly refused.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass


class QuestionShape(str, enum.Enum):
    TERMINAL = "TERMINAL"
    BARRIER_ABOVE = "BARRIER_ABOVE"
    BARRIER_BELOW = "BARRIER_BELOW"
    RANGE = "RANGE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ShapeResult:
    shape: QuestionShape
    lower: float | None = None
    upper: float | None = None
    reason: str = ""

    @property
    def is_modelable(self) -> bool:
        if self.shape is QuestionShape.UNKNOWN:
            return False
        if self.shape is QuestionShape.RANGE:
            return self.lower is not None and self.upper is not None
        return self.lower is not None or self.upper is not None

    @property
    def threshold(self) -> float | None:
        """The single level the question turns on, where there is one."""
        if self.shape is QuestionShape.RANGE:
            return None
        return self.lower if self.lower is not None else self.upper


_MONEY = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kmb])?\b", re.I)

# Ordered most-specific first. A "between" question also contains "above"-ish
# words, so range must be tested before anything else.
_RANGE = re.compile(r"\bbetween\b.{0,40}?\band\b|\bfrom\b.{0,25}?\bto\b(?!\s*\$?\d+\s*(?:by|on|before))", re.I)

# "dip to", "fall to", "drop to" — the path must touch a level below.
_BARRIER_BELOW = re.compile(
    r"\b(dip|fall|drop|decline|sink|crash|retrace|pull ?back)\s+(to|below|under)\b"
    r"|\bhit\s+(?:a\s+)?low\b"
    r"|\btouch\b.{0,15}\bbelow\b",
    re.I,
)

# "reach", "hit", "climb to" — the path must touch a level above.
# No trailing preposition group: "reach $65,000" has none, and requiring a word
# boundary after an optional group failed to match before a "$".
_BARRIER_ABOVE = re.compile(
    r"\b(reach|reaches|hit|hits|touch|touches|climb|climbs|rise|rises"
    r"|surge|surges|rally|rallies|jump|jumps|spike|spikes)\b",
    re.I,
)

# "be above X on DATE", "close above", "end the month above".
_TERMINAL = re.compile(
    r"\b(be|close|end|finish|settle)\b.{0,25}\b(above|below|over|under|greater than|less than|at least|at most)\b"
    r"|\b(above|below|over|under)\b.{0,20}\b(on|at|by end of|at the close)\b",
    re.I,
)

_DIRECTION_ABOVE = re.compile(
    r"\b(above|over|exceed|greater than|higher than|more than|at least)\b", re.I
)
_DIRECTION_BELOW = re.compile(
    r"\b(below|under|less than|lower than|fewer than|at most|dip|fall|drop|decline)\b", re.I
)

# Questions this module must refuse: they compare against a value that is not
# stated in the text, so no threshold can be extracted at all.
_UNMODELABLE = re.compile(
    r"\bup or down\b|\bhigher or lower\b|\bgreen or red\b"
    r"|\bcompared to\b|\bvs\.?\b|\bversus\b"
    r"|\bpercent(age)? (increase|decrease|change)\b"
    r"|\bnew all[- ]time high\b",
    re.I,
)


def _parse_amounts(text: str) -> list[float]:
    """Every monetary amount in the text, in order of appearance."""
    amounts: list[float] = []
    for match in _MONEY.finditer(text):
        try:
            value = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        suffix = (match.group(2) or "").lower()
        multiplier = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(suffix, 1)
        amounts.append(value * multiplier)
    return amounts


def detect_shape(question: str | None) -> ShapeResult:
    """Classify a price question's shape and extract its level(s).

    Conservative by design: an unclear question returns UNKNOWN, and the crypto
    model then declines to produce an estimate. A wrong shape is far more
    damaging than a missing one, because it yields a confident wrong number
    rather than an honest silence.
    """
    if not question or not question.strip():
        return ShapeResult(QuestionShape.UNKNOWN, reason="no question text")

    text = question.strip()

    if _UNMODELABLE.search(text):
        return ShapeResult(
            QuestionShape.UNKNOWN,
            reason=(
                "question compares against an unstated reference (e.g. the opening "
                "price), so no threshold can be extracted"
            ),
        )

    amounts = _parse_amounts(text)
    if not amounts:
        return ShapeResult(QuestionShape.UNKNOWN, reason="no monetary threshold found")

    # -- range: needs exactly two bounds ---------------------------------
    if _RANGE.search(text):
        if len(amounts) < 2:
            return ShapeResult(
                QuestionShape.UNKNOWN,
                reason="range question but only one bound could be parsed",
            )
        lower, upper = sorted(amounts[:2])
        return ShapeResult(
            QuestionShape.RANGE, lower=lower, upper=upper, reason="range: between two bounds"
        )

    # More than one amount outside a range question is ambiguous — we cannot
    # tell which one the question turns on.
    if len(amounts) > 1 and len(set(amounts)) > 1:
        return ShapeResult(
            QuestionShape.UNKNOWN,
            reason=f"{len(amounts)} distinct amounts found but no range wording; ambiguous",
        )

    level = amounts[0]

    # -- barrier below ---------------------------------------------------
    if _BARRIER_BELOW.search(text):
        return ShapeResult(
            QuestionShape.BARRIER_BELOW, lower=level,
            reason="barrier: price must touch a level below at some point",
        )

    # -- terminal (checked before barrier-above, because "be above X on DATE"
    #    contains no barrier verb but "rise above X by DATE" does) ---------
    if _TERMINAL.search(text) and not _BARRIER_ABOVE.search(text):
        if _DIRECTION_BELOW.search(text) and not _DIRECTION_ABOVE.search(text):
            return ShapeResult(
                QuestionShape.TERMINAL, upper=level,
                reason="terminal: price below a level at resolution",
            )
        return ShapeResult(
            QuestionShape.TERMINAL, lower=level,
            reason="terminal: price above a level at resolution",
        )

    # -- barrier above ---------------------------------------------------
    if _BARRIER_ABOVE.search(text):
        return ShapeResult(
            QuestionShape.BARRIER_ABOVE, lower=level,
            reason="barrier: price must touch a level above at some point",
        )

    # A bare direction word with a level, and no verb telling us whether the
    # path or only the endpoint matters. Terminal is the conventional reading.
    if _DIRECTION_ABOVE.search(text):
        return ShapeResult(
            QuestionShape.TERMINAL, lower=level,
            reason="terminal (assumed): direction word with no path verb",
        )
    if _DIRECTION_BELOW.search(text):
        return ShapeResult(
            QuestionShape.TERMINAL, upper=level,
            reason="terminal (assumed): direction word with no path verb",
        )

    return ShapeResult(
        QuestionShape.UNKNOWN,
        reason="a level was found but the question's shape could not be determined",
    )
