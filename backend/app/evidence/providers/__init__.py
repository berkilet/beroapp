"""Evidence connectors, resolved from the source registry.

The factory is the only place that maps a registry key to an implementation.
Nothing else in the system imports a specific provider, so adding a source is a
registry entry plus one line here.
"""

from __future__ import annotations

from app.evidence.base import EvidenceProvider
from app.evidence.providers.bls import BLSProvider
from app.evidence.providers.crypto import CoinbaseExchangeProvider, KrakenProvider
from app.evidence.providers.fec import FECProvider
from app.evidence.providers.fomc import FOMCCalendarProvider
from app.evidence.providers.sec import SECEdgarProvider
from app.evidence.providers.treasury import (
    TreasuryFiscalDataProvider,
    TreasuryYieldCurveProvider,
)
from app.evidence.registry import BY_KEY, SourceDefinition

PROVIDER_CLASSES: dict[str, type[EvidenceProvider]] = {
    "treasury_yield_curve": TreasuryYieldCurveProvider,
    "treasury_fiscal_data": TreasuryFiscalDataProvider,
    "bls": BLSProvider,
    "fomc_calendar": FOMCCalendarProvider,
    "sec_edgar": SECEdgarProvider,
    "coinbase_exchange": CoinbaseExchangeProvider,
    "kraken": KrakenProvider,
    "fec": FECProvider,
}


def build_provider(source_key: str, fetcher, settings) -> EvidenceProvider | None:
    """Instantiate a connector, or None when the source has no implementation."""
    definition: SourceDefinition | None = BY_KEY.get(source_key)
    provider_class = PROVIDER_CLASSES.get(source_key)
    if definition is None or provider_class is None or not definition.implemented:
        return None
    return provider_class(definition, fetcher, settings)


def build_enabled_providers(fetcher, settings) -> list[EvidenceProvider]:
    """Every connector whose source is implemented and currently enabled."""
    from app.evidence.registry import SOURCES, is_enabled

    providers: list[EvidenceProvider] = []
    for definition in SOURCES:
        enabled, _ = is_enabled(definition, settings)
        if not enabled:
            continue
        provider = build_provider(definition.source_key, fetcher, settings)
        if provider is not None:
            providers.append(provider)
    return providers


__all__ = [
    "BLSProvider",
    "CoinbaseExchangeProvider",
    "FECProvider",
    "FOMCCalendarProvider",
    "KrakenProvider",
    "SECEdgarProvider",
    "TreasuryFiscalDataProvider",
    "TreasuryYieldCurveProvider",
    "build_enabled_providers",
    "build_provider",
]
