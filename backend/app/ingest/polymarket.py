"""Polymarket API client.

One method per verified endpoint. Every method validates its response through
the schemas in ``app.schemas.polymarket`` and reports malformed records rather
than swallowing them.

Endpoint choices, and why, are recorded in docs/DATA_SOURCES.md. In short:

* **Gamma** for discovery and metadata.
* **CLOB** for anything that affects an execution decision. Gamma's bid/ask are
  stored only as reference values.
* **Data** for corroborating activity (open interest, public prints).
* Batch endpoints are used wherever one exists — ``POST /books`` turns N
  per-market requests into ceil(N/50).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.ingest.http import FetchError, HttpFetcher
from app.schemas.polymarket import (
    GammaEvent,
    GammaMarket,
    MalformedRecord,
    OrderBook,
    PriceHistoryPoint,
)

log = get_logger("ingest.polymarket")


@dataclass
class ParseReport:
    """Counts of what survived validation. Surfaced as a system event so a
    silent rise in malformed records is visible rather than invisible."""

    accepted: int = 0
    rejected: int = 0
    reasons: list[str] = field(default_factory=list)

    def reject(self, reason: str) -> None:
        self.rejected += 1
        if len(self.reasons) < 20:
            self.reasons.append(reason[:300])

    @property
    def error_rate(self) -> float:
        total = self.accepted + self.rejected
        return self.rejected / total if total else 0.0


class PolymarketClient:
    def __init__(self, fetcher: HttpFetcher | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._owns_fetcher = fetcher is None
        self.fetcher = fetcher or HttpFetcher(self.settings)

    async def aclose(self) -> None:
        if self._owns_fetcher:
            await self.fetcher.aclose()

    async def __aenter__(self) -> "PolymarketClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Gamma — discovery and metadata
    # ------------------------------------------------------------------
    async def list_events(
        self,
        *,
        closed: bool | None = False,
        limit: int | None = None,
        offset: int = 0,
        order: str = "volume",
        ascending: bool = False,
    ) -> tuple[list[GammaEvent], ParseReport]:
        """GET /events. Events carry the tags that drive classification."""
        params: dict[str, Any] = {
            "limit": limit or self.settings.discovery_page_size,
            "offset": offset,
            "order": order,
            "ascending": str(ascending).lower(),
        }
        if closed is not None:
            params["closed"] = str(closed).lower()

        payload = await self.fetcher.fetch_json(f"{self.settings.gamma_base_url}/events", params=params)
        report = ParseReport()

        if not isinstance(payload, list):
            raise MalformedRecord(
                f"Gamma /events returned {type(payload).__name__}, expected a list. "
                "This is a schema change, not a transient failure."
            )

        events: list[GammaEvent] = []
        for raw in payload:
            try:
                events.append(GammaEvent.model_validate(raw))
                report.accepted += 1
            except Exception as exc:  # noqa: BLE001
                report.reject(f"event: {exc}")
        return events, report

    async def list_markets(
        self,
        *,
        closed: bool | None = False,
        limit: int | None = None,
        offset: int = 0,
        order: str = "volumeNum",
        ascending: bool = False,
        condition_ids: list[str] | None = None,
    ) -> tuple[list[GammaMarket], ParseReport]:
        """GET /markets."""
        params: dict[str, Any] = {
            "limit": limit or self.settings.discovery_page_size,
            "offset": offset,
            "order": order,
            "ascending": str(ascending).lower(),
        }
        if closed is not None:
            params["closed"] = str(closed).lower()
        if condition_ids:
            params["condition_ids"] = condition_ids

        payload = await self.fetcher.fetch_json(
            f"{self.settings.gamma_base_url}/markets", params=params
        )
        report = ParseReport()

        if not isinstance(payload, list):
            raise MalformedRecord(
                f"Gamma /markets returned {type(payload).__name__}, expected a list."
            )

        markets: list[GammaMarket] = []
        for raw in payload:
            try:
                markets.append(GammaMarket.model_validate(raw))
                report.accepted += 1
            except Exception as exc:  # noqa: BLE001
                report.reject(f"market: {exc}")
        return markets, report

    async def get_markets_by_condition_ids(
        self, condition_ids: list[str]
    ) -> tuple[list[GammaMarket], ParseReport]:
        """Fetch specific markets, used by the resolution worker to re-check
        markets we already know about (including closed ones)."""
        if not condition_ids:
            return [], ParseReport()
        return await self.list_markets(
            closed=None, condition_ids=condition_ids, limit=len(condition_ids)
        )

    # ------------------------------------------------------------------
    # CLOB — microstructure
    # ------------------------------------------------------------------
    async def get_books(self, token_ids: list[str]) -> tuple[list[OrderBook], ParseReport]:
        """POST /books — the batch order-book endpoint.

        This is the single most important call in the system: it is where
        executable prices come from, and using the batch form is what keeps the
        request budget proportional to ceil(N/50) rather than N.
        """
        report = ParseReport()
        if not token_ids:
            return [], report

        payload = await self.fetcher.fetch_json(
            f"{self.settings.clob_base_url}/books",
            method="POST",
            json_body=[{"token_id": t} for t in token_ids],
        )
        if not isinstance(payload, list):
            raise MalformedRecord(
                f"CLOB /books returned {type(payload).__name__}, expected a list."
            )

        books: list[OrderBook] = []
        for raw in payload:
            try:
                books.append(OrderBook.model_validate(raw))
                report.accepted += 1
            except Exception as exc:  # noqa: BLE001
                report.reject(f"book: {exc}")
        return books, report

    async def get_book(self, token_id: str) -> OrderBook:
        """GET /book — single book. Diagnostics and tests."""
        payload = await self.fetcher.fetch_json(
            f"{self.settings.clob_base_url}/book", params={"token_id": token_id}
        )
        return OrderBook.model_validate(payload)

    async def get_midpoints(self, token_ids: list[str]) -> dict[str, float]:
        """POST /midpoints. Used as an independent cross-check on the book."""
        if not token_ids:
            return {}
        payload = await self.fetcher.fetch_json(
            f"{self.settings.clob_base_url}/midpoints",
            method="POST",
            json_body=[{"token_id": t} for t in token_ids],
        )
        if not isinstance(payload, dict):
            raise MalformedRecord("CLOB /midpoints returned a non-object payload")

        out: dict[str, float] = {}
        for token, value in payload.items():
            try:
                price = float(value)
            except (TypeError, ValueError):
                continue
            if 0.0 <= price <= 1.0:
                out[str(token)] = price
        return out

    async def get_price_history(
        self, token_id: str, *, interval: str = "1d", fidelity: int = 60
    ) -> list[PriceHistoryPoint]:
        """GET /prices-history. Backfill for markets discovered mid-life."""
        payload = await self.fetcher.fetch_json(
            f"{self.settings.clob_base_url}/prices-history",
            params={"market": token_id, "interval": interval, "fidelity": fidelity},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("history"), list):
            raise MalformedRecord("CLOB /prices-history returned an unexpected shape")

        points: list[PriceHistoryPoint] = []
        for raw in payload["history"]:
            try:
                points.append(PriceHistoryPoint.model_validate(raw))
            except Exception:  # noqa: BLE001
                continue
        return points

    async def get_server_time(self) -> int | None:
        """GET /time — returns a bare epoch-seconds integer, not JSON.

        Used for clock-skew detection, which feeds CONNECTIVITY_KILL_SWITCH.
        """
        try:
            payload = await self.fetcher.fetch_json(f"{self.settings.clob_base_url}/time")
        except FetchError:
            return None
        try:
            return int(payload)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Data API — corroborating activity
    # ------------------------------------------------------------------
    async def get_open_interest(self, condition_id: str) -> float | None:
        """GET /oi. Returns None when unknown — never 0 as a stand-in."""
        try:
            payload = await self.fetcher.fetch_json(
                f"{self.settings.data_base_url}/oi", params={"market": condition_id}
            )
        except FetchError:
            return None
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            try:
                return float(payload[0].get("value"))
            except (TypeError, ValueError):
                return None
        return None

    async def get_recent_trades(self, condition_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """GET /trades — public prints, not our trades."""
        try:
            payload = await self.fetcher.fetch_json(
                f"{self.settings.data_base_url}/trades",
                params={"market": condition_id, "limit": limit},
            )
        except FetchError:
            return []
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []
