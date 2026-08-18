# PHASE_GATES.md

Phases do not advance on their own. There is no timer, no profitability
threshold, and no code path that promotes a phase automatically. Each transition
requires a human to run an explicit command after reading evidence the system
computed from stored observations.

Current phase is stored in `system_config` and reported at `/api/system/phase`.
A fresh install is **PHASE_1**.

---

## Phase 1 — Prediction engine

**What runs:** discovery, snapshotting, classification, modelability scoring,
probability estimation, edge computation, risk evaluation, recommendation
storage, resolution tracking.

**What does not run:** any portfolio simulation, any execution of any kind.

**Money at risk: none. There is no execution adapter instantiated in this phase.**

### Gate: Phase 1 → Phase 2

Every criterion below is evaluated by `scripts/phase_gate.py --target 2`, which
queries the database and prints a pass/fail table. It writes a
`phase_gate_evaluation` audit row. It does **not** change the phase.

| # | Criterion | Config key | Default | Checked by |
|---|---|---|---|---|
| 1 | Distinct markets observed | `GATE1_MIN_MARKETS` | 250 | count of `markets` with ≥1 snapshot |
| 2 | Predictions stored | `GATE1_MIN_PREDICTIONS` | 1000 | count of `predictions` |
| 3 | Resolved markets with a prediction made ≥24 h before resolution | `GATE1_MIN_RESOLVED` | 50 | join `predictions` × `resolutions` |
| 4 | Continuous uptime observed | `GATE1_MIN_UPTIME_DAYS` | 14 | `system_events` heartbeat coverage |
| 5 | Snapshot gap ratio below threshold | `GATE1_MAX_GAP_RATIO` | 0.05 | missed vs expected snapshot cycles |
| 6 | Parse-error rate below threshold | `GATE1_MAX_PARSE_ERROR_RATE` | 0.01 | `system_events` |
| 7 | Calibration analysis completed and stored | — | — | a `performance_metrics` row of kind `calibration` computed over ≥ `GATE1_MIN_RESOLVED` outcomes |
| 8 | Brier score beats the always-market baseline | — | — | model Brier < market-implied Brier on the same resolved set |
| 9 | Security test suite passes | — | — | `pytest tests/security -q` recorded green in the gate run |
| 10 | Documented model performance exists | — | — | a `model_versions` row with a non-null `performance_summary` |

Criterion 8 deserves emphasis. A model that is merely *well-calibrated* is not
useful — the market is also well-calibrated. The gate requires the model to be
**better than the market it is trying to beat**, measured out-of-sample on
resolved markets. If it is not, Phase 2 is pointless and the gate says so.

**To advance:** `python scripts/phase_gate.py --target 2 --confirm`
Refuses unless all criteria pass. Writes an audit row naming the operator.

---

## Phase 2 — Shadow trading

**What additionally runs:** the paper execution engine, virtual portfolio
accounting, P&L, drawdown, and per-bucket performance attribution.

**Money at risk: none.** `VIRTUAL_INITIAL_CAPITAL` (default 10000) is virtual.
The dashboard labels every figure derived from it **VIRTUAL / PAPER CAPITAL**.
It is never described as, converted to, or compared against real funds.

Paper fills are simulated against the recorded order book at the recorded
timestamp — walking real depth, applying real spread, modelling latency between
signal and execution, and allowing partial fills. A paper fill is **never** booked
at the signal price.

### Gate: Phase 2 → Phase 3

`scripts/phase_gate.py --target 3` evaluates everything below. This gate is
deliberately harder to pass than the previous one, and passing it still does not
enable live trading — it only makes enabling it *possible*.

| # | Criterion | Config key | Default |
|---|---|---|---|
| 1 | Phase 1 gate previously passed and recorded | — | — |
| 2 | Paper trades executed | `GATE2_MIN_PAPER_TRADES` | 300 |
| 3 | Paper trades that reached resolution | `GATE2_MIN_SETTLED_TRADES` | 150 |
| 4 | Continuous Phase 2 operation | `GATE2_MIN_DAYS` | 60 |
| 5 | Out-of-sample Brier score | `GATE2_MAX_BRIER` | 0.24 |
| 6 | Expected calibration error | `GATE2_MAX_ECE` | 0.05 |
| 7 | Expectancy per trade, after modelled slippage and fees | `GATE2_MIN_EXPECTANCY` | > 0 |
| 8 | Maximum drawdown | `GATE2_MAX_DRAWDOWN` | 0.20 |
| 9 | Realised slippage within modelled bounds | `GATE2_MAX_SLIPPAGE_ERROR` | 0.02 |
| 10 | Signal→execution latency within budget | `GATE2_MAX_LATENCY_MS` | 5000 |
| 11 | Opportunity persistence: edge still present after model latency | `GATE2_MIN_PERSISTENCE` | 0.60 |

Then the manual checklist, each item requiring an explicit `--ack` flag and each
recorded individually in `audit_logs` with operator and timestamp:

- [ ] `--ack phase1-complete`
- [ ] `--ack phase2-complete`
- [ ] `--ack sample-size-reviewed`
- [ ] `--ack calibration-reviewed`
- [ ] `--ack brier-reviewed`
- [ ] `--ack drawdown-reviewed`
- [ ] `--ack expectancy-reviewed`
- [ ] `--ack liquidity-assumptions-reviewed`
- [ ] `--ack slippage-assumptions-reviewed`
- [ ] `--ack security-review-complete`
- [ ] `--ack api-permissions-reviewed`
- [ ] `--ack risk-limits-configured`
- [ ] `--ack kill-switches-tested`
- [ ] `--ack live-execution-authorised`

Criterion 11 is the one most backtests omit and the one most likely to fail. An
edge that has evaporated by the time the model finishes computing is not an edge.
The platform measures this directly by re-reading the book after the signal and
recording whether the executable edge survived.

---

## Phase 3 — Controlled live trading

**Not implemented as a working path, and not enabled.** `LIVE_TRADING_ENABLED`
defaults to `false`. The live adapter refuses to construct unless *all* of the
following hold simultaneously:

1. `LIVE_TRADING_ENABLED=true` in the environment,
2. the phase-3 gate is recorded as passed in the database,
3. all fourteen acknowledgements above are present,
4. an operator authorisation row exists and has not expired,
5. every hard risk limit below is set to a concrete value — no defaults, no nulls.

### Hard limits (deterministic, enforced in code, not overridable by any model)

| Limit | Meaning |
|---|---|
| `MAX_POSITION_SIZE_PERCENT` | Cap on a single position as % of equity |
| `MAX_MARKET_EXPOSURE_PERCENT` | Cap on total exposure to one market |
| `MAX_PORTFOLIO_EXPOSURE_PERCENT` | Cap on total deployed capital |
| `MAX_DAILY_LOSS_PERCENT` | Trips `RISK_KILL_SWITCH` for the day |
| `MAX_DRAWDOWN_PERCENT` | Trips `RISK_KILL_SWITCH` until manually reset |
| `MAX_CORRELATED_EXPOSURE_PERCENT` | Cap across markets sharing an event or neg-risk group |
| `MIN_LIQUIDITY` | Refuse markets thinner than this |
| `MAX_SPREAD` | Refuse markets wider than this |
| `MAX_ALLOWED_SLIPPAGE` | Refuse a fill worse than this vs. expectation |

No LLM, no model, and no API caller can modify these. They are read from the
frozen settings object and there is no setter.

### Kill switches

Five, independent, all fail **closed** — the safe state on any error, on any
unknown state, and at process start is *tripped*.

| Switch | Trips when |
|---|---|
| `GLOBAL_KILL_SWITCH` | Operator action; halts all trading activity |
| `DATA_KILL_SWITCH` | Any required feed older than `DATA_STALENESS_S` |
| `MODEL_KILL_SWITCH` | Model version unregistered, output invalid, or calibration drift beyond threshold |
| `RISK_KILL_SWITCH` | Daily loss or drawdown limit breached |
| `CONNECTIVITY_KILL_SWITCH` | Consecutive API failures or clock skew beyond tolerance |

A tripped switch can be reset only by an operator, only through an audited
action, and never automatically.

### Capital

The system enforces the configured limits. It does **not** advise how much
capital to deposit, does not suggest an amount, and does not compute a
"recommended" balance. That is not a software decision.

---

## What passing a gate does and does not mean

Passing a gate means the recorded observations satisfied thresholds chosen in
advance. It does not mean the model is good, that past performance will persist,
that the slippage model is right, or that live results will resemble paper
results. Those thresholds were picked as defensible starting points, not derived
from theory.

The honest summary: these gates are designed to stop an obviously-broken system
from reaching real money. They cannot certify a working one.
