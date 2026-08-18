"""Federal Election Commission connector.

Candidate registry data: who has actually filed to run, for which office, in
which cycle. That is a genuinely useful structural fact for election markets —
a market on a candidate who has not filed is pricing something different from a
market on one who has.

**What this is not.** FEC publishes campaign finance and registration data, not
polling and not outcomes. Nothing here forecasts an election. Treating a
candidate's fundraising as a probability would be exactly the kind of unearned
inference this platform refuses to make, so the connector emits registry facts
and the feature layer uses them only as eligibility signals.

Requires a free API key. The public ``DEMO_KEY`` makes the endpoint reachable
for a one-off check but is limited to 30 requests/hour, so the connector refuses
to fall back to it: pointing continuous polling at a shared demo credential is
unreliable and discourteous. Without ``FEC_API_KEY`` the source reports DISABLED.

Verified reachable 2026-08-18 (with DEMO_KEY, manually).
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.enums import (
    ComponentHealth,
    EvidenceType,
    MarketCategory,
    MarketSubcategory,
    SourceType,
    VerificationStatus,
)
from app.evidence.base import EvidenceError, EvidenceItem, EvidenceProvider
from app.ingest.http import FetchError

OFFICE_LABELS = {"P": "President", "S": "Senate", "H": "House"}


class FECProvider(EvidenceProvider):
    """Declared federal candidates for the current and next cycle."""

    @property
    def request_cost(self) -> int:
        return 1

    async def collect(self, *, now: datetime | None = None) -> list[EvidenceItem]:
        now = now or datetime.now(UTC)
        started = datetime.now(UTC)

        key = self.settings.fec_api_key.get_secret_value()
        if not key:
            # No silent fallback to the shared DEMO_KEY. It is limited to 30
            # requests/hour, and pointing continuous automated polling at a
            # public demo credential is both unreliable and poor citizenship.
            # The registry already reports this source DISABLED without a key.
            raise EvidenceError(
                "FEC_API_KEY is not set; refusing to fall back to the shared DEMO_KEY",
                source_key=self.source_key, error_code="missing_api_key",
            )
        # Federal cycles are even years; the next one is what markets trade.
        cycle = now.year + (now.year % 2)

        try:
            payload = await self.fetcher.fetch_json(
                f"{self.definition.base_url}/candidates/",
                params={
                    "api_key": key,
                    "cycle": cycle,
                    "office": "P",
                    "candidate_status": "C",
                    "per_page": 100,
                    "sort": "name",
                },
                headers=self._headers(),
            )
        except FetchError as exc:
            self._record_health(ComponentHealth.FAILED, str(exc)[:200], error_code=exc.error_code)
            raise EvidenceError(
                f"FEC fetch failed: {exc}", source_key=self.source_key, error_code=exc.error_code
            ) from exc

        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise EvidenceError(
                "FEC returned an unexpected shape",
                source_key=self.source_key, error_code="schema",
            )

        items: list[EvidenceItem] = []
        for record in results:
            if not isinstance(record, dict):
                continue
            name = record.get("name")
            candidate_id = record.get("candidate_id")
            if not name or not candidate_id:
                continue

            office = OFFICE_LABELS.get(str(record.get("office", "")), "Unknown")
            surname = str(name).split(",")[0].strip().lower()

            items.append(
                EvidenceItem(
                    source_key=self.source_key,
                    source_type=SourceType.OFFICIAL_GOVERNMENT,
                    source_tier=1,
                    evidence_type=EvidenceType.REGISTRY_RECORD,
                    series_key=f"FEC_CANDIDATE_{candidate_id}",
                    title=f"{name} is a registered {office} candidate for cycle {cycle}",
                    numeric_value=None,
                    unit=None,
                    observation_date=datetime(cycle, 1, 1, tzinfo=UTC),
                    known_at=now,
                    reference_url=f"https://www.fec.gov/data/candidate/{candidate_id}/",
                    verification_status=VerificationStatus.CONFIRMED_FACT,
                    reliability_score=self.definition.reliability_score,
                    parser_version=self.definition.parser_version,
                    payload={
                        "candidate_id": candidate_id,
                        "name": name,
                        "office": office,
                        "party": record.get("party_full"),
                        "cycle": cycle,
                        "status": record.get("candidate_status"),
                        "note": "registration record only; not a forecast of any outcome",
                    },
                    subject_tags=tuple(
                        t for t in (surname, "candidate", "election", office.lower()) if t
                    ),
                    categories=(MarketCategory.ELECTIONS, MarketCategory.POLITICS),
                    subcategories=(
                        MarketSubcategory.US_PRESIDENTIAL,
                        MarketSubcategory.US_PRIMARY,
                    ),
                )
            )

        latency = int((datetime.now(UTC) - started).total_seconds() * 1000)
        self._record_health(
            ComponentHealth.HEALTHY if items else ComponentHealth.DEGRADED,
            f"{len(items)} registered candidates for cycle {cycle}",
            items=len(items), latency_ms=latency,
        )
        return items
