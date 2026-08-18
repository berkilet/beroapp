# beroapp

A self-hosted, continuously-running research platform for Polymarket prediction
markets. It discovers the market universe, samples order books, forms an
independent probability estimate, computes **executable** edge after spread and
depth, and keeps an audit trail sufficient to reconstruct any decision.

It is a measurement instrument. Its purpose is to answer, honestly, whether the
probability engine can find persistent, executable inefficiencies — not to
trade.

---

## Status, stated plainly

**Live trading is disabled and Phase 3 is not implemented.** `LIVE_TRADING_ENABLED`
defaults to `false`, and the live adapter contains no venue client, no signing
code, and no credential handling. Four independent guards would all have to fail
before a real order could be placed.

| Phase | What runs | Status |
|---|---|---|
| **Phase 1** — prediction engine | discovery, snapshots, classification, modelability, probability, edge, risk, resolution, calibration | **working** |
| **Phase 2** — shadow trading | paper execution against recorded books, portfolio, P&L, drawdown | engine and adapter implemented and tested; **not enabled** (requires the Phase 1 gate) |
| **Phase 3** — live trading | isolated live adapter | **architectural capability only, deliberately not implemented** |

What this system does **not** yet have, and you should know before running it:

* **No external evidence is ingested.** The source registry, provenance schema
  and health reporting exist, but no connector is written. The only genuinely
  independent signal the model has is Polymarket's own neg-risk coherence
  constraint. Everything else defers to the market. See `docs/DATA_SOURCES.md`.
* **No trained model.** Learned estimators activate only after
  `MIN_TRAINING_OBSERVATIONS` (default 500) markets have resolved. Until then
  the interpretable baseline is the only estimator, by design — an untrained
  model would be a fabricated one.
* **No performance claim.** Nothing in this repository asserts profitability,
  calibration quality, or security. Every such figure is computed from stored
  observations, and reports `insufficient_data` when the sample is too small.

---

## What it does

```
 Polymarket (Gamma / CLOB / Data)
        │
        ▼
 ingestion ── validate ── store (every row carries known_at)
        │
        ▼
 modelability filter ── most markets are excluded, with reasons
        │
        ▼
 probability engine ── independent estimate + uncertainty
        │
        ▼
 edge engine ── raw → executable → liquidity-adjusted → risk-adjusted
        │
        ▼
 risk engine ── deterministic limits + five fail-closed kill switches
        │
        ▼
 execution authorization ── the only minter of execution tokens
        │
        ▼
 paper adapter (Phase 2)          live adapter (refuses to construct)
```

The discipline the whole system is built around: **classify on the executable
edge, never the raw one.** A model at 64% against a market at 56% is an 8-point
raw edge, but if the real fill is at 59% it is a 5-point edge, and if the book
only holds $70 it is not an opportunity at all.

---

## Requirements

* Python 3.11+
* PostgreSQL 16 (local; no hosted or paid database)
* Node.js 20+
* macOS for the `launchd` 24/7 configuration (the services themselves are
  platform-neutral)

Everything is open-source. There is no paid dependency and no cloud requirement.

---

## Quick start

```bash
# 1. Database
createdb beroapp
psql -c "CREATE ROLE beroapp LOGIN PASSWORD '<generated>'"

# 2. Backend
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env          # then edit: DATABASE_URL and API_KEY
.venv/bin/alembic upgrade head
psql -d beroapp -f ../ops/grants.sql
PYTHONPATH=. .venv/bin/python scripts/seed.py

# 3. Run
.venv/bin/python -m app.workers.main &                       # ingestion + computation
.venv/bin/uvicorn app.api.main:app --host 127.0.0.1 --port 8000 &

# 4. Dashboard
cd ../frontend
npm ci
cp .env.local.example .env.local   # set BACKEND_API_KEY to the same value
npm run build && npm start
```

Then open <http://127.0.0.1:3000/dashboard>.

Generate an API key with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

For 24/7 operation with automatic restart after reboot, see
`docs/OPERATIONS.md`.

---

## Verifying it works

```bash
cd backend
.venv/bin/pytest                      # 295 tests
bash ../scripts/security_scan.sh      # tests, bandit, pip-audit, ruff, secret scan
PYTHONPATH=. .venv/bin/python scripts/phase_gate.py --target 2
```

The phase gate will fail on a fresh install, and it is supposed to. It reports
exactly which criteria are short.

---

## Layout

```
backend/
  app/
    core/        settings (frozen), enums, structured logging with redaction
    db/          models, session, migrations
    ingest/      hardened HTTP client, Polymarket client, persistence
    schemas/     strict validation for untrusted venue data
    engines/     classification, modelability, liquidity, probability, edge,
                 risk, kill switches, authorization, calibration
    execution/   paper adapter; live adapter that refuses to construct
    workers/     supervisor + discovery, snapshot, prediction, resolution, metrics
    api/         read-only FastAPI surface, auth, security middleware, health
  scripts/       phase_gate.py, seed.py
  tests/         unit, integration, security
frontend/        Next.js dashboard, 13 pages
ops/             launchd jobs, database grants
scripts/         backup, restore, security scan
docs/            architecture, data sources, security, phase gates, operations,
                 model card, backtesting
```

---

## Design commitments

These are the rules the code is written to, and most of them have a test that
fails if they stop holding.

1. **No fabricated data.** A value that is unknown is stored and displayed as
   unknown. Nullable numeric columns mean "not measured", never zero.
2. **No look-ahead bias.** Every fact-bearing row carries `known_at`, and the
   prediction path filters on it. A backtest runs the same code with an earlier
   `as_of`, so look-ahead is structurally impossible rather than merely avoided.
3. **No survivorship bias.** Closed, resolved, cancelled and invalid markets are
   retained forever. Nothing is deleted for being uninteresting.
4. **No blind trust in LLMs.** The probability engine is deterministic statistics
   and runs with no LLM configured. An LLM cannot produce a number that reaches
   the edge engine, and cannot import the execution or risk packages.
5. **No direct LLM-to-trade path.** Only the authorization service mints an
   execution token, and adapters refuse tokens they did not receive from it.
6. **Fail closed.** Stale data, unknown risk state, invalid model output, an
   unmeasured clock — each produces *fewer* signals, never more. All five kill
   switches default to tripped.
7. **Resolution is never inferred from price.** A market at 0.995 is a market at
   0.995. An unclear settlement is recorded `AMBIGUOUS` and excluded from
   calibration rather than guessed.

---

## Documentation

| Document | Contents |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Process topology, worker pipeline, decision chain, trust boundaries, failure model |
| [DATA_SOURCES.md](docs/DATA_SOURCES.md) | Every endpoint, verified against the official OpenAPI specs and live responses, with rate limits and known hazards |
| [SECURITY.md](docs/SECURITY.md) | Threat model, the money-loss boundary, prompt-injection defence, and an honest list of limitations |
| [PHASE_GATES.md](docs/PHASE_GATES.md) | What must be true before a phase advances, and what passing does not mean |
| [OPERATIONS.md](docs/OPERATIONS.md) | Install, 24/7 setup, health checks, backup and restore, troubleshooting |
| [MODEL_CARD.md](docs/MODEL_CARD.md) | What the baseline does, what it cannot do, and its known failure modes |
| [BACKTESTING.md](docs/BACKTESTING.md) | How look-ahead and survivorship bias are prevented, and walk-forward methodology |

---

## Licence

See [LICENSE](LICENSE).

This software is a research tool. It makes no representation that any signal it
produces has value, and nothing in it constitutes financial advice.
