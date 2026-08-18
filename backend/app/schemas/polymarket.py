"""Validation schemas for Polymarket responses.

Everything the venue sends is untrusted input. These models are the boundary
where it becomes typed data. Two rules apply throughout:

* A field we cannot parse becomes ``None``, never a substituted default. A
  missing liquidity figure is *unknown*, not zero, and downstream code must
  decide what to do about not knowing.
* A record whose *structural* invariants fail (no token IDs, outcome/token
  arity mismatch, price outside [0,1]) is rejected entirely. Partial ingestion
  of a market is worse than no ingestion, because it looks like data.

The field shapes here were verified against live responses on 2026-08-18; see
docs/DATA_SOURCES.md for the recorded evidence, including the encoding hazard
that ``outcomes``/``outcomePrices``/``clobTokenIds`` arrive as JSON-encoded
strings rather than arrays.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PARSER_VERSION = "polymarket-parser/1.0.0"


class MalformedRecord(ValueError):
    """Raised when a record cannot be trusted enough to store."""


def _to_float(value: Any) -> float | None:
    """Parse a number that may arrive as a float, an int, or a string.

    Returns None for anything unparseable, and rejects NaN/infinity outright —
    those propagate silently through arithmetic and poison everything they touch.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            result = float(stripped)
        except ValueError:
            return None
    else:
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def _to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _decode_json_list(value: Any) -> list[Any] | None:
    """Decode the JSON-encoded-string arrays Gamma returns.

    Accepts a real list too, so the parser keeps working if the venue ever
    fixes the encoding.
    """
    if value is None:
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            decoded = json.loads(stripped)
        except (ValueError, TypeError):
            return None
        return decoded if isinstance(decoded, list) else None
    return None


class GammaTag(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    label: str | None = None
    slug: str | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _stringify(cls, v: Any) -> str | None:
        return None if v is None else str(v)


class GammaMarket(BaseModel):
    """A market as returned by Gamma ``/markets`` or embedded in an event."""

    model_config = ConfigDict(extra="ignore")

    gamma_market_id: str = Field(alias="id")
    condition_id: str = Field(alias="conditionId")
    question_id: str | None = Field(default=None, alias="questionID")
    slug: str | None = None
    question: str | None = None
    description: str | None = None
    group_item_title: str | None = Field(default=None, alias="groupItemTitle")

    # Untrusted free text. Stored as data; never interpreted as instruction.
    resolution_source: str | None = Field(default=None, alias="resolutionSource")
    resolved_by: str | None = Field(default=None, alias="resolvedBy")
    uma_resolution_statuses: list[Any] | None = Field(default=None, alias="umaResolutionStatuses")

    outcomes: list[str] = Field(default_factory=list)
    outcome_prices: list[float] | None = Field(default=None, alias="outcomePrices")
    clob_token_ids: list[str] = Field(default_factory=list, alias="clobTokenIds")

    active: bool | None = None
    closed: bool | None = None
    archived: bool | None = None
    accepting_orders: bool | None = Field(default=None, alias="acceptingOrders")
    enable_order_book: bool | None = Field(default=None, alias="enableOrderBook")
    neg_risk: bool | None = Field(default=None, alias="negRisk")
    neg_risk_market_id: str | None = Field(default=None, alias="negRiskMarketID")

    liquidity_num: float | None = Field(default=None, alias="liquidityNum")
    volume_num: float | None = Field(default=None, alias="volumeNum")
    volume_24hr: float | None = Field(default=None, alias="volume24hr")
    order_min_size: float | None = Field(default=None, alias="orderMinSize")
    tick_size: float | None = Field(default=None, alias="orderPriceMinTickSize")

    # Gamma's own bid/ask. Reference metadata only — executable prices come
    # from the CLOB book. Kept so we can measure how far the two diverge.
    reference_best_bid: float | None = Field(default=None, alias="bestBid")
    reference_best_ask: float | None = Field(default=None, alias="bestAsk")
    reference_spread: float | None = Field(default=None, alias="spread")
    last_trade_price: float | None = Field(default=None, alias="lastTradePrice")

    start_date: datetime | None = Field(default=None, alias="startDate")
    end_date: datetime | None = Field(default=None, alias="endDate")
    source_created_at: datetime | None = Field(default=None, alias="createdAt")
    source_updated_at: datetime | None = Field(default=None, alias="updatedAt")

    event_ids: list[str] = Field(default_factory=list)

    @field_validator("gamma_market_id", "condition_id", "question_id", mode="before")
    @classmethod
    def _stringify(cls, v: Any) -> str | None:
        return None if v is None else str(v)

    @field_validator(
        "liquidity_num", "volume_num", "volume_24hr", "order_min_size", "tick_size",
        "reference_best_bid", "reference_best_ask", "reference_spread", "last_trade_price",
        mode="before",
    )
    @classmethod
    def _numeric(cls, v: Any) -> float | None:
        return _to_float(v)

    @field_validator(
        "start_date", "end_date", "source_created_at", "source_updated_at", mode="before"
    )
    @classmethod
    def _temporal(cls, v: Any) -> datetime | None:
        return _to_datetime(v)

    @field_validator("outcomes", "clob_token_ids", mode="before")
    @classmethod
    def _string_list(cls, v: Any) -> list[str]:
        decoded = _decode_json_list(v)
        return [str(item) for item in decoded] if decoded else []

    @field_validator("outcome_prices", mode="before")
    @classmethod
    def _price_list(cls, v: Any) -> list[float] | None:
        decoded = _decode_json_list(v)
        if not decoded:
            return None
        parsed = [_to_float(item) for item in decoded]
        return None if any(p is None for p in parsed) else [p for p in parsed if p is not None]

    @field_validator("uma_resolution_statuses", mode="before")
    @classmethod
    def _uma(cls, v: Any) -> list[Any] | None:
        return _decode_json_list(v)

    @model_validator(mode="before")
    @classmethod
    def _lift_event_ids(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            data = dict(data)
            data["event_ids"] = [
                str(e["id"]) for e in data["events"] if isinstance(e, dict) and e.get("id") is not None
            ]
        return data

    @model_validator(mode="after")
    def _structural_invariants(self) -> "GammaMarket":
        if not self.clob_token_ids:
            raise MalformedRecord(
                f"market {self.gamma_market_id} has no CLOB token ids; cannot price it"
            )
        if self.outcomes and len(self.outcomes) != len(self.clob_token_ids):
            raise MalformedRecord(
                f"market {self.gamma_market_id}: {len(self.outcomes)} outcomes but "
                f"{len(self.clob_token_ids)} tokens"
            )
        if len(set(self.clob_token_ids)) != len(self.clob_token_ids):
            raise MalformedRecord(f"market {self.gamma_market_id} has duplicate token ids")
        if self.outcome_prices is not None:
            if len(self.outcome_prices) != len(self.clob_token_ids):
                raise MalformedRecord(
                    f"market {self.gamma_market_id}: outcome price arity mismatch"
                )
            if any(not 0.0 <= p <= 1.0 for p in self.outcome_prices):
                raise MalformedRecord(
                    f"market {self.gamma_market_id}: outcome price outside [0,1]"
                )
        return self

    @property
    def is_binary(self) -> bool:
        return len(self.clob_token_ids) == 2


class GammaEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    gamma_event_id: str = Field(alias="id")
    ticker: str | None = None
    slug: str | None = None
    title: str | None = None
    description: str | None = None
    tags: list[GammaTag] = Field(default_factory=list)

    neg_risk: bool | None = Field(default=None, alias="negRisk")
    active: bool | None = None
    closed: bool | None = None
    archived: bool | None = None

    liquidity: float | None = None
    volume: float | None = None
    open_interest: float | None = Field(default=None, alias="openInterest")

    start_date: datetime | None = Field(default=None, alias="startDate")
    end_date: datetime | None = Field(default=None, alias="endDate")
    source_updated_at: datetime | None = Field(default=None, alias="updatedAt")

    markets: list[GammaMarket] = Field(default_factory=list)

    @field_validator("gamma_event_id", mode="before")
    @classmethod
    def _stringify(cls, v: Any) -> str | None:
        return None if v is None else str(v)

    @field_validator("liquidity", "volume", "open_interest", mode="before")
    @classmethod
    def _numeric(cls, v: Any) -> float | None:
        return _to_float(v)

    @field_validator("start_date", "end_date", "source_updated_at", mode="before")
    @classmethod
    def _temporal(cls, v: Any) -> datetime | None:
        return _to_datetime(v)

    @field_validator("markets", mode="before")
    @classmethod
    def _drop_malformed_markets(cls, v: Any) -> Any:
        """A bad market inside an event must not discard the whole event.

        Each embedded market is validated independently; unparseable ones are
        dropped and counted by the caller, never silently repaired.
        """
        if not isinstance(v, list):
            return []
        kept = []
        for raw in v:
            try:
                kept.append(GammaMarket.model_validate(raw))
            except Exception:  # noqa: BLE001 - malformed record, counted upstream
                continue
        return kept


class BookLevel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    price: float
    size: float

    @field_validator("price", "size", mode="before")
    @classmethod
    def _numeric(cls, v: Any) -> float:
        parsed = _to_float(v)
        if parsed is None:
            raise MalformedRecord(f"unparseable book level value {v!r}")
        return parsed

    @model_validator(mode="after")
    def _bounds(self) -> "BookLevel":
        if not 0.0 <= self.price <= 1.0:
            raise MalformedRecord(f"book price {self.price} outside [0,1]")
        if self.size < 0:
            raise MalformedRecord(f"negative book size {self.size}")
        return self


class OrderBook(BaseModel):
    """A CLOB order book.

    Level ordering is *not* trusted: observed responses had bids ascending and
    asks descending, so best bid is computed as max(bid price) and best ask as
    min(ask price). Relying on element order here would be a live bug the day
    the venue changes its sort.
    """

    model_config = ConfigDict(extra="ignore")

    token_id: str = Field(alias="asset_id")
    condition_id: str | None = Field(default=None, alias="market")
    observed_at: datetime | None = Field(default=None, alias="timestamp")
    book_hash: str | None = Field(default=None, alias="hash")
    bids: list[BookLevel] = Field(default_factory=list)
    asks: list[BookLevel] = Field(default_factory=list)
    tick_size: float | None = None
    min_order_size: float | None = None
    neg_risk: bool | None = None
    last_trade_price: float | None = None

    @field_validator("token_id", "condition_id", "book_hash", mode="before")
    @classmethod
    def _stringify(cls, v: Any) -> str | None:
        return None if v is None else str(v)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _millis(cls, v: Any) -> datetime | None:
        """Venue sends epoch milliseconds as a string."""
        raw = _to_float(v)
        if raw is None:
            return None
        seconds = raw / 1000.0 if raw > 1e11 else raw
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None

    @field_validator("tick_size", "min_order_size", "last_trade_price", mode="before")
    @classmethod
    def _numeric(cls, v: Any) -> float | None:
        return _to_float(v)

    @field_validator("bids", "asks", mode="before")
    @classmethod
    def _levels(cls, v: Any) -> Any:
        if not isinstance(v, list):
            return []
        return v

    @model_validator(mode="after")
    def _crossed_book_is_malformed(self) -> "OrderBook":
        bid, ask = self.best_bid, self.best_ask
        if bid is not None and ask is not None and bid > ask:
            # A crossed book is either a venue bug or a corrupted response.
            # Either way it must not reach the edge engine.
            raise MalformedRecord(
                f"crossed book for token {self.token_id}: bid {bid} > ask {ask}"
            )
        return self

    @property
    def best_bid(self) -> float | None:
        return max((lvl.price for lvl in self.bids), default=None)

    @property
    def best_ask(self) -> float | None:
        return min((lvl.price for lvl in self.asks), default=None)

    @property
    def midpoint(self) -> float | None:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2.0

    @property
    def spread(self) -> float | None:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return ask - bid

    def depth_usd(self, side: str) -> float:
        """Notional value resting on one side, in USD.

        Polymarket share sizes are in shares; a share costs its price in USD, so
        notional is sum(price * size).
        """
        levels = self.bids if side == "bid" else self.asks
        return sum(lvl.price * lvl.size for lvl in levels)

    @property
    def imbalance(self) -> float | None:
        """(bid - ask) / (bid + ask) notional. Positive means buy pressure."""
        bid_usd, ask_usd = self.depth_usd("bid"), self.depth_usd("ask")
        total = bid_usd + ask_usd
        if total <= 0:
            return None
        return (bid_usd - ask_usd) / total


class PriceHistoryPoint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    t: datetime
    p: float

    @field_validator("t", mode="before")
    @classmethod
    def _epoch(cls, v: Any) -> datetime | None:
        return _to_datetime(v)

    @field_validator("p", mode="before")
    @classmethod
    def _price(cls, v: Any) -> float:
        parsed = _to_float(v)
        if parsed is None or not 0.0 <= parsed <= 1.0:
            raise MalformedRecord(f"price history point outside [0,1]: {v!r}")
        return parsed
