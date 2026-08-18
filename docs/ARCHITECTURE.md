# ARCHITECTURE.md

## 1. What this system is

A self-hosted, continuously-running research platform that:

1. discovers the Polymarket market universe,
2. samples market microstructure (order books) on a schedule,
3. forms an **independent** probability estimate for markets it can model,
4. computes **executable** edge after spread, depth, slippage and fees,
5. emits structured, validated recommendations,
6. simulates execution against a virtual portfolio,
7. measures its own calibration and financial performance,
8. and keeps an append-only audit trail sufficient to reconstruct any decision.

It is a measurement instrument first. Its purpose is to answer, with statistical
honesty, whether the probability engine finds *persistent, executable*
inefficiencies. It is not optimised for trade count or headline profit.

## 2. What this system is not

* Not a live trading bot. `LIVE_TRADING_ENABLED` defaults to `false` and Phase 1
  and Phase 2 contain **no code path** that can reach an exchange order endpoint.
* Not an LLM wrapper. The probability engine is deterministic statistics and runs
  with no LLM configured. The optional LLM layer only restructures already-ingested
  text and can never produce the number a trade is sized on.
* Not a source of truth about the future. Every probability it emits carries an
  uncertainty and a model version, and every claim it makes about its own
  performance is computed from stored observations, never asserted.

## 3. Process topology

The system runs as three long-lived processes plus PostgreSQL. On macOS each is
a `launchd` job (see `OPERATIONS.md`).

```
                          ┌──────────────────────┐
                          │   PostgreSQL 16      │
                          │   (local, on-disk)   │
                          └───────┬──────────────┘
                                  │
        ┌─────────────────────────┼─────────────────────────┐
        │                         │                         │
┌───────┴────────┐      ┌─────────┴─────────┐     ┌─────────┴────────┐
│  api           │      │  worker           │     │  frontend        │
│  FastAPI       │      │  async scheduler  │     │  Next.js         │
│  read-mostly   │      │  all ingestion    │     │  browser client  │
│  :8000         │      │  and computation  │     │  :3000           │
└────────────────┘      └───────────────────┘     └──────────────────┘
```

**The API process performs no ingestion and no model inference.** It reads what
the worker has already written. This is deliberate: a slow or hostile HTTP
request cannot delay data collection, and the worker's cadence is independent of
whether anyone is looking at the dashboard.

**The frontend never talks to Polymarket.** It talks only to the API. That keeps
the browser out of the trust boundary entirely (see §7).

## 4. Worker pipeline

The worker runs a set of independently-scheduled, independently-failing jobs. One
job crashing does not stop the others; each is wrapped in a supervisor that logs,
backs off, and retries.

```
 discovery          every DISCOVERY_INTERVAL_S (default 900s)
   Gamma /markets + /events  →  markets, events, market_tokens tables
   classification            →  category
   modelability scoring      →  modelability status + score

 snapshot           every SNAPSHOT_INTERVAL_S (default 60s)
   CLOB POST /books (batched)  →  order_book_snapshots, market_snapshots
   change detection            →  skip write when nothing material changed
   staleness detection         →  DATA feed health

 prediction         every PREDICTION_INTERVAL_S (default 300s)
   feature assembly (known_at filtered)
   probability engine  →  predictions
   edge engine         →  signals
   risk engine         →  risk_decisions
   paper engine        →  paper_orders, paper_fills, positions

 resolution         every RESOLUTION_INTERVAL_S (default 1800s)
   Gamma closed/resolved status + UMA fields  →  resolutions
   position settlement, realised P&L

 metrics            every METRICS_INTERVAL_S (default 600s)
   calibration, Brier, log-loss, drawdown, portfolio_snapshots
```

Every job writes a `system_events` heartbeat row on both success and failure.
Health is derived from those rows, not from an in-memory flag, so health survives
a process restart and reflects reality rather than optimism.

## 5. The decision pipeline

This is the required execution boundary, implemented as a one-directional chain
of modules with no back-edges:

```
   DATA (ingest/)
     │   markets, order books, evidence — all with known_at
     ▼
   PROBABILITY ENGINE (engines/probability.py)
     │   model_probability, uncertainty, model_version
     ▼
   EDGE ENGINE (engines/edge.py)
     │   raw → executable → liquidity-adjusted → risk-adjusted edge
     ▼
   RISK ENGINE (engines/risk.py)
     │   deterministic limits + five kill switches; APPROVED | REJECTED
     ▼
   EXECUTION AUTHORIZATION (engines/authorization.py)
     │   the only component that may mint an execution token
     ▼
   EXECUTION ADAPTER (execution/paper.py | execution/live.py)
     │   paper adapter: always available
     │   live adapter: import-guarded, refuses to construct in Phase 1/2
     ▼
   MARKET
```

Enforced properties:

* `engines/probability.py` imports nothing from `execution/`. There is a test
  that asserts this by static import analysis, so the property cannot regress
  silently.
* The LLM layer (`engines/llm/`) can be called only from evidence extraction. It
  cannot import `execution/`, `engines/risk.py`, or the settings object that
  holds limits.
* The API exposes no route that creates an order of any kind. The dashboard is
  read-only plus a small set of operator controls (kill switches — which can only
  ever move toward *safer*).

## 6. The probability engine

Layered so that the system is honest about what it does and does not know.

**Layer 0 — market-implied probability.** Derived from the CLOB book, not from
Gamma's display price. For a binary YES token, `market_probability` is the
midpoint of best bid and best ask; the *executable* probability is the price the
order would actually cross at, computed by walking the book for the intended
size.

**Layer 1 — baseline model.** A deterministic, interpretable estimator per
category. The shipped baseline (`v0.1.0-baseline`) combines:
* a time-decay prior toward the market on short horizons,
* a cross-sectional prior from the event's sibling markets (for neg-risk event
  groups the sibling YES prices must sum to ~1; a violation is a real, arbitrage-
  adjacent signal that is measurable without any external data),
* a microstructure term from order-book imbalance,
* explicit shrinkage toward the market proportional to model uncertainty.

The last term matters: absent genuine external evidence the model should mostly
agree with the market, and the baseline is built so that it *does*. A model that
disagrees loudly with the market on no evidence is not clever, it is broken.

**Layer 2 — calibrated statistical models.** `scikit-learn` logistic regression
and gradient-boosted trees with isotonic/Platt calibration, trained only on
resolved markets via walk-forward splits. These become active only once
`MIN_TRAINING_OBSERVATIONS` resolved markets exist. Until then the system reports
`INSUFFICIENT_DATA` for model-driven signals rather than shipping an untrained
model. **On a fresh install this is the normal state and the dashboard says so.**

**Layer 3 — optional LLM evidence enrichment.** Only for markets that already
passed the cheap filters. Produces structured evidence rows, never a probability.

Any model output that is non-numeric, outside [0,1], NaN, infinite, missing
required fields, or tagged with an unregistered model version is **rejected** and
recorded as a `model_rejection` system event. It is never replaced with a guess.

## 7. Trust boundaries

```
 ┌─ UNTRUSTED ─────────────────────────────────────────────┐
 │ Polymarket JSON, market descriptions, resolution text,  │
 │ any external feed, any LLM output, the browser          │
 └─────────────────────────┬───────────────────────────────┘
                           │ strict Pydantic validation,
                           │ text is stored as data and never
                           │ concatenated into an instruction
 ┌─────────────────────────┴───────────────────────────────┐
 │ TRUSTED: worker computation, risk engine, database      │
 └─────────────────────────────────────────────────────────┘
```

External text (market `description`, `resolutionSource`, news bodies) is treated
as data everywhere. Where it is passed to an LLM it goes inside a delimited,
clearly-labelled user-content block, the system prompt states that content in
that block is untrusted data, and the response must validate against a strict
schema whose fields are all enumerated or numeric — there is no free-text field
in the LLM response that any downstream code acts on. See `SECURITY.md` §
Prompt injection.

## 8. Data model

Twenty tables, grouped:

* **Universe:** `events`, `markets`, `market_tokens`
* **Market data:** `market_snapshots`, `order_book_snapshots`, `trades`
* **Evidence:** `external_sources`, `external_events`
* **Decisions:** `predictions`, `signals`, `risk_decisions`
* **Simulation:** `paper_orders`, `paper_fills`, `positions`, `portfolio_snapshots`
* **Outcome:** `resolutions`
* **Models:** `model_versions`, `model_predictions`, `performance_metrics`
* **Operations:** `system_events`, `audit_logs`

Design rules:

* Every fact-bearing row has both an *event time* and a `known_at`. The
  backtester filters exclusively on `known_at`, which is what makes look-ahead
  bias structurally impossible rather than merely discouraged.
* Snapshot tables are append-only and written **only on material change**
  (`SNAPSHOT_MIN_CHANGE`), so a quiet market costs no rows.
* `audit_logs` and `system_events` are append-only by convention and by grant:
  the application role has `INSERT`/`SELECT` but no `UPDATE`/`DELETE` on them
  (see `ops/grants.sql`).
* The universe tables retain closed, resolved, cancelled and invalid markets
  forever. Nothing is ever deleted for being uninteresting — that is what
  prevents survivorship bias.

## 9. Failure model

| Failure | Response |
|---|---|
| Polymarket 429 | Honour `Retry-After`; token-bucket limiter already keeps us far below documented ceilings |
| Polymarket 5xx | Exponential backoff with jitter, capped; circuit breaker opens after N consecutive failures |
| Malformed payload | Reject the record, increment a parse-error counter, emit `system_event`; never partially ingest |
| Schema change | Pydantic validation fails loudly → feed marked `FAILED`, not silently degraded |
| Stale data | Feed marked `STALE` past `DATA_STALENESS_S`; `DATA_KILL_SWITCH` trips; no signals emitted |
| DB down | Connection pool with pre-ping and reconnect; worker jobs retry with backoff; API returns 503 from `/readiness` |
| Worker crash | `launchd` `KeepAlive` restarts it; jobs are idempotent so a mid-cycle death is safe |
| macOS reboot | `RunAtLoad` starts PostgreSQL, API, worker in dependency order |

Every one of these is **fail-closed**: the degraded state produces *fewer*
signals, never more.

## 10. Technology and why

| Choice | Reason |
|---|---|
| Python 3.11 + FastAPI | Async ingestion, Pydantic validation at the boundary is the same tool as the API schema |
| PostgreSQL 16, local | Free, durable, good at time-series-ish append workloads with partial indexes |
| SQLAlchemy 2.0 + Alembic | Typed ORM; migrations are reviewable artifacts |
| httpx | Async, connection reuse, per-host limits |
| scikit-learn / NumPy / pandas | Interpretable models first; calibration tooling is first-class |
| Next.js + TypeScript + Tailwind | Required by spec; server components keep the API key out of the browser |
| pytest | Everything, including the security assertions |

No paid dependency. No hosted database. No cloud requirement.
