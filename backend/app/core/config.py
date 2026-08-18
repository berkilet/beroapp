"""Application configuration.

Every tunable lives here. Nothing in this module has a side effect beyond
reading the environment, and the resulting settings object is frozen: there is
no supported way to mutate a limit or a safety flag at runtime.

The defaults are chosen so that an operator who runs the system with an empty
environment gets the *safest* configuration, not the most capable one.
"""

from __future__ import annotations

import functools
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Phase = Literal["PHASE_1", "PHASE_2", "PHASE_3"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # ------------------------------------------------------------------
    # Identity / runtime
    # ------------------------------------------------------------------
    app_name: str = "beroapp"
    environment: Literal["local", "test", "production"] = "local"
    debug: bool = False
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000

    # ------------------------------------------------------------------
    # SAFETY. Read this block before changing anything in it.
    # ------------------------------------------------------------------
    live_trading_enabled: bool = False
    """Master switch for real-money execution. MUST default to False.

    Setting this to True is necessary but nowhere near sufficient: the live
    adapter additionally requires recorded phase gates, operator authorisation,
    and concrete values for every hard risk limit. See docs/PHASE_GATES.md.
    """

    current_phase: Phase = "PHASE_1"

    # Kill switches. All default to TRIPPED (True) because fail-closed means the
    # unknown state is the stopped state. The worker clears the automatic ones
    # once it has positively verified the corresponding condition is healthy.
    global_kill_switch: bool = True
    data_kill_switch: bool = True
    model_kill_switch: bool = True
    risk_kill_switch: bool = True
    connectivity_kill_switch: bool = True

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    database_url: str = "postgresql+psycopg://beroapp:beroapp@127.0.0.1:5432/beroapp"
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_statement_timeout_ms: int = 30_000

    # ------------------------------------------------------------------
    # API security
    # ------------------------------------------------------------------
    api_key: SecretStr = SecretStr("")
    operator_api_key: SecretStr = SecretStr("")
    allow_insecure_local: bool = False
    """Permits running with no API key. Development only; refused when the
    server is bound to anything other than loopback."""

    cors_allow_origins: tuple[str, ...] = ("http://127.0.0.1:3000", "http://localhost:3000")
    max_request_bytes: int = 64 * 1024
    api_rate_limit_per_minute: int = 240
    operator_rate_limit_per_minute: int = 20

    # ------------------------------------------------------------------
    # Polymarket endpoints. Hosts are also the SSRF allow-list.
    # ------------------------------------------------------------------
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    clob_base_url: str = "https://clob.polymarket.com"
    data_base_url: str = "https://data-api.polymarket.com"
    polymarket_user_agent: str = "beroapp-research/0.1 (self-hosted prediction-market research)"

    # Request budgets, deliberately far below the documented ceilings recorded
    # in docs/DATA_SOURCES.md. We have no need for burst throughput.
    gamma_rps: float = 3.0
    clob_rps: float = 5.0
    data_rps: float = 2.0

    http_connect_timeout_s: float = 5.0
    http_read_timeout_s: float = 20.0
    http_total_timeout_s: float = 30.0
    http_max_retries: int = 4
    http_backoff_base_s: float = 1.0
    http_backoff_max_s: float = 30.0
    circuit_breaker_failures: int = 8
    circuit_breaker_reset_s: float = 120.0

    book_batch_size: int = 50
    discovery_page_size: int = 100
    discovery_max_pages: int = 60

    # ------------------------------------------------------------------
    # Worker cadence
    # ------------------------------------------------------------------
    discovery_interval_s: int = 900
    snapshot_interval_s: int = 60
    prediction_interval_s: int = 300
    resolution_interval_s: int = 1800
    metrics_interval_s: int = 600
    heartbeat_interval_s: int = 30

    data_staleness_s: int = 300
    """Beyond this age the market-data feed is STALE and DATA_KILL_SWITCH trips."""

    snapshot_min_price_change: float = 0.001
    """Skip writing a snapshot when nothing moved by at least this much. Keeps a
    quiet market from costing a row per cycle."""

    max_clock_skew_s: float = 60.0

    # ------------------------------------------------------------------
    # Modelability filter
    # ------------------------------------------------------------------
    min_liquidity: float = 5_000.0
    max_spread: float = 0.05
    min_hours_to_resolution: float = 6.0
    max_days_to_resolution: float = 400.0
    min_market_age_hours: float = 24.0

    # ------------------------------------------------------------------
    # Probability / edge
    # ------------------------------------------------------------------
    baseline_model_version: str = "v0.1.0-baseline"
    min_training_observations: int = 500
    """Below this many resolved markets the learned models stay inactive and the
    system reports INSUFFICIENT_DATA rather than shipping an untrained model."""

    min_executable_edge: float = 0.02
    min_confidence: float = 0.55
    reference_order_size_usd: float = 500.0
    max_allowed_slippage: float = 0.02

    # ------------------------------------------------------------------
    # Paper trading (Phase 2). Virtual only.
    # ------------------------------------------------------------------
    virtual_initial_capital: float = 10_000.0
    paper_latency_ms: int = 1_500
    paper_fee_bps: float = 0.0
    """Polymarket binary markets observed with feesEnabled=false; kept
    configurable rather than assumed to be zero forever."""

    # ------------------------------------------------------------------
    # Hard risk limits. Deterministic. No model may override these.
    # ------------------------------------------------------------------
    max_position_size_percent: float = 2.0
    max_market_exposure_percent: float = 5.0
    max_portfolio_exposure_percent: float = 50.0
    max_daily_loss_percent: float = 5.0
    max_drawdown_percent: float = 20.0
    max_correlated_exposure_percent: float = 15.0

    # ------------------------------------------------------------------
    # Optional LLM layer. Absent by default; the system is fully functional
    # without it.
    # ------------------------------------------------------------------
    llm_enabled: bool = False
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "claude-sonnet-5"
    llm_max_markets_per_cycle: int = 10

    # Optional evidence-source keys. Absent -> connector reports DISABLED.
    fred_api_key: SecretStr = SecretStr("")
    sec_user_agent: str = ""

    # ------------------------------------------------------------------
    # Phase gate thresholds
    # ------------------------------------------------------------------
    gate1_min_markets: int = 250
    gate1_min_predictions: int = 1_000
    gate1_min_resolved: int = 50
    gate1_min_uptime_days: int = 14
    gate1_max_gap_ratio: float = 0.05
    gate1_max_parse_error_rate: float = 0.01
    gate2_min_paper_trades: int = 300
    gate2_min_settled_trades: int = 150
    gate2_min_days: int = 60
    gate2_max_brier: float = 0.24
    gate2_max_ece: float = 0.05
    gate2_min_expectancy: float = 0.0
    gate2_max_drawdown: float = 0.20
    gate2_max_slippage_error: float = 0.02
    gate2_max_latency_ms: int = 5_000
    gate2_min_persistence: float = 0.60

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------
    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return tuple(part.strip() for part in v.split(",") if part.strip())
        return v

    @model_validator(mode="after")
    def _enforce_safe_combinations(self) -> "Settings":
        loopback = self.bind_host in {"127.0.0.1", "localhost", "::1"}

        if self.debug and not loopback:
            raise ValueError(
                "debug=true is refused when bind_host is not loopback: it would risk "
                "leaking internals to a non-local client"
            )

        if not loopback and self.allow_insecure_local:
            raise ValueError(
                "allow_insecure_local=true is refused when bind_host is not loopback"
            )

        if not self.api_key.get_secret_value() and not self.allow_insecure_local:
            raise ValueError(
                "API_KEY is empty. Set one, or set ALLOW_INSECURE_LOCAL=true for "
                "loopback development."
            )

        if self.live_trading_enabled and self.current_phase != "PHASE_3":
            raise ValueError(
                "LIVE_TRADING_ENABLED=true requires CURRENT_PHASE=PHASE_3. Refusing "
                "to start in a configuration where live execution is armed before "
                "the phase gates have been recorded."
            )

        if not 0 < self.snapshot_interval_s <= self.data_staleness_s:
            raise ValueError(
                "snapshot_interval_s must be positive and no greater than "
                "data_staleness_s, otherwise the feed is stale by construction"
            )

        return self

    # ------------------------------------------------------------------
    # Derived
    # ------------------------------------------------------------------
    @property
    def allowed_outbound_hosts(self) -> frozenset[str]:
        """SSRF allow-list. The HTTP client refuses any other host."""
        from urllib.parse import urlparse

        hosts = {
            urlparse(u).hostname
            for u in (self.gamma_base_url, self.clob_base_url, self.data_base_url)
        }
        return frozenset(h for h in hosts if h)

    @property
    def paper_trading_active(self) -> bool:
        return self.current_phase in ("PHASE_2", "PHASE_3")


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
