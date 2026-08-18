"""Evidence provider interface.

Every connector implements this, and nothing else in the system knows what a
particular source looks like. The worker asks providers for evidence; providers
translate a foreign API into `EvidenceItem`s and know nothing about markets,
features or models.

Two properties are enforced here rather than left to each connector:

* **Isolation.** A provider that raises does not take down the cycle. The worker
  catches per-provider, records the failure against that source, and continues.
  Treasury being down must not stop crypto ingestion.
* **Provenance.** An `EvidenceItem` cannot be constructed without a source, a
  parser version, and the three timestamps that make look-ahead detectable.
"""

from __future__ import annotations

import abc
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.enums import (
    ComponentHealth,
    EvidenceType,
    MarketCategory,
    MarketSubcategory,
    SourceType,
    VerificationStatus,
)
from app.evidence.registry import SourceDefinition


class EvidenceError(Exception):
    """A provider could not produce evidence. Isolated by the worker."""

    def __init__(self, message: str, *, source_key: str, error_code: str) -> None:
        super().__init__(message)
        self.source_key = source_key
        self.error_code = error_code


@dataclass(frozen=True)
class EvidenceItem:
    """One observed fact, with everything needed to reproduce and date it.

    The three timestamps are deliberately separate and none may be inferred
    from another:

    * ``observation_date`` — the period the measurement describes (July CPI)
    * ``published_at``     — when the issuing body released it (mid-August)
    * ``known_at``         — when this platform could first use it

    Conflating the first two is how a backtest ends up "knowing" July's CPI
    during July. Conflating the last two is how it ends up knowing a figure
    before it was published.
    """

    source_key: str
    source_type: SourceType
    source_tier: int
    evidence_type: EvidenceType

    series_key: str
    """Stable name for a repeated measurement. Two items sharing a series_key
    and observation_date are competing claims about the same fact."""

    title: str
    known_at: datetime
    parser_version: str

    numeric_value: float | None = None
    unit: str | None = None
    observation_date: datetime | None = None
    published_at: datetime | None = None

    reference_url: str | None = None
    verification_status: VerificationStatus = VerificationStatus.CONFIRMED_FACT
    reliability_score: float = 0.9
    payload: dict = field(default_factory=dict)

    subject_tags: tuple[str, ...] = ()
    """Lowercase tokens used by the matcher, e.g. ("cpi", "inflation", "btc")."""

    categories: tuple[MarketCategory, ...] = ()
    subcategories: tuple[MarketSubcategory, ...] = ()

    def __post_init__(self) -> None:
        if self.known_at.tzinfo is None:
            raise ValueError("known_at must be timezone-aware")
        if self.published_at is not None and self.published_at > self.known_at:
            # We cannot know something before it was published. A provider that
            # produces this has mis-parsed a timestamp, and letting it through
            # would inject look-ahead at the source.
            raise ValueError(
                f"{self.series_key}: published_at {self.published_at} is after "
                f"known_at {self.known_at}"
            )
        if self.numeric_value is not None:
            value = float(self.numeric_value)
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError(f"{self.series_key}: numeric_value is not finite")

    @property
    def content_hash(self) -> str:
        """Identity of the *fact*, not of the fetch.

        Deliberately excludes known_at and ingestion time so that re-fetching an
        unchanged observation deduplicates, while a revision — a different value
        for the same period — hashes differently and is stored as a new row.
        """
        material = json.dumps(
            {
                "source": self.source_key,
                "series": self.series_key,
                "observation_date": (
                    self.observation_date.isoformat() if self.observation_date else None
                ),
                "value": self.numeric_value,
                "unit": self.unit,
                "title": self.title,
                "published_at": self.published_at.isoformat() if self.published_at else None,
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode()).hexdigest()

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or datetime.now(UTC)) - self.known_at).total_seconds()


@dataclass
class ProviderHealth:
    source_key: str
    health: ComponentHealth
    message: str
    items_produced: int = 0
    latency_ms: int | None = None
    error_code: str | None = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict:
        return {
            "source_key": self.source_key,
            "health": self.health.value,
            "message": self.message,
            "items_produced": self.items_produced,
            "latency_ms": self.latency_ms,
            "error_code": self.error_code,
            "checked_at": self.checked_at.isoformat(),
        }


class EvidenceProvider(abc.ABC):
    """Base class for every evidence connector."""

    def __init__(self, definition: SourceDefinition, fetcher, settings) -> None:
        self.definition = definition
        self.fetcher = fetcher
        self.settings = settings
        self._last_health = ProviderHealth(
            source_key=definition.source_key,
            health=ComponentHealth.UNKNOWN,
            message="not yet run",
        )

    @property
    def source_key(self) -> str:
        return self.definition.source_key

    @property
    def request_cost(self) -> int:
        """Requests one collection cycle consumes, for daily-budget accounting.

        Providers that batch report the batched cost — the BLS connector costs 1
        because it fetches every series it needs in a single POST.
        """
        return 1

    @abc.abstractmethod
    async def collect(self, *, now: datetime | None = None) -> list[EvidenceItem]:
        """Fetch and normalise. Raises EvidenceError on failure.

        Must not swallow errors into an empty list: an empty result means "this
        source genuinely has nothing new", and a failure must be distinguishable
        from that.
        """

    async def get_latest_updates(self, *, now: datetime | None = None) -> list[EvidenceItem]:
        """Items new since the last cycle. Defaults to a full collect; the
        worker deduplicates on content hash, so this is correct if wasteful."""
        return await self.collect(now=now)

    def get_health(self) -> ProviderHealth:
        return self._last_health

    def _record_health(
        self,
        health: ComponentHealth,
        message: str,
        *,
        items: int = 0,
        latency_ms: int | None = None,
        error_code: str | None = None,
    ) -> ProviderHealth:
        self._last_health = ProviderHealth(
            source_key=self.source_key,
            health=health,
            message=message,
            items_produced=items,
            latency_ms=latency_ms,
            error_code=error_code,
        )
        return self._last_health

    # -- helpers shared by connectors -----------------------------------
    def _headers(self) -> dict[str, str]:
        """Identifying User-Agent on every outbound request.

        Never a browser impersonation. SEC in particular requires a contactable
        identity, and sending an anonymous request would breach its policy.
        """
        agent = self.settings.evidence_user_agent
        if self.definition.source_key == "sec_edgar" and self.settings.sec_user_agent:
            agent = self.settings.sec_user_agent
        return {"User-Agent": agent, "Accept": "application/json"}
