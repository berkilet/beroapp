# BACKTESTING.md

## Implementation status

The replay runner and walk-forward splitter are implemented
(`backend/app/backtest/`). Model *fitting* is not, and cannot be: no market this
system predicted has resolved yet, so there is nothing to fit on.

That distinction matters. Most backtesting frameworks fail not because the
simulation loop is wrong but because the *data* permits look-ahead. The
groundwork below is what makes an honest backtest possible at all.

| Component | Status |
|---|---|
| `known_at` on every fact-bearing row | **implemented** |
| Prediction path filtered on `known_at <= as_of` | **implemented** |
| Full order-book snapshots retained for fill reconstruction | **implemented** |
| Closed/resolved/cancelled markets retained (no survivorship bias) | **implemented** |
| Immutable input references on every prediction | **implemented** |
| Fill simulation against a recorded book | **implemented** (`execution/paper.py`) |
| Replay runner calling the production engines at a historical `as_of` | **implemented** (`backtest/runner.py`) |
| Empirical no-look-ahead check | **implemented** (`verify_no_lookahead`) |
| Walk-forward splitter with purging, embargo and event grouping | **implemented** (`backtest/walkforward.py`) |
| Per-category training-readiness reporting | **implemented** |
| Model fitting | **not implemented** — no resolved-market history yet |

### The runner replays production code, not a copy of it

`BacktestRunner.replay()` calls the same classification, feature-building,
category-model, probability, edge and risk code the live worker calls, with
`as_of` set to a past instant. There is no separate "backtest implementation" of
the model that could drift from the live one — a class of bug that is
essentially undetectable once it exists.

Every read inside that path filters on `known_at <= as_of`. The runner does not
enforce this from outside; the engines enforce it themselves, which is why the
same code can serve both purposes.

### Verifying the claim rather than asserting it

`verify_no_lookahead()` re-runs a replay and checks that no row contributing to
a prediction carries a `known_at` later than the `as_of` it was produced for.
Structural arguments about look-ahead are worth making, but they are not worth
trusting on their own: look-ahead is invisible when it happens and catastrophic
when it does.

A representative run over recorded history: 96 replay points, 0 errors, 0
look-ahead violations. The point of quoting that number is not that it is large
— it is small — but that the machinery runs end to end on real recorded data.

---

## 1. Why look-ahead is structurally impossible here

Every fact-bearing row carries two timestamps:

* **event time** — when the thing happened in the world (`observed_at`,
  `published_at`, `traded_at`, `resolved_at`)
* **`known_at`** — the earliest moment this platform could legitimately use it

They are not the same, and conflating them is the single most common way a
backtest lies. A market resolved at 14:00 but recorded at 14:37 has
`resolved_at = 14:00` and `known_at = 14:37`. A backtest at 14:15 must not see
it.

The prediction worker takes an `as_of` parameter and filters **every** query on
`known_at <= as_of`:

```python
async def run_once(self, *, as_of: datetime | None = None, ...):
    as_of = as_of or datetime.now(UTC)
    contexts = self._load_contexts(session, as_of=as_of)
```

In live operation `as_of` is now, so the filter is a no-op. In a backtest it is a
historical timestamp — and **the same code path runs**. There is no separate
"backtest mode" that could drift out of sync with production, which is the other
common way a backtest lies.

Each stored prediction also carries `input_refs`, naming the exact
`market_snapshot_id` and `order_book_snapshot_id` it used. Any historical
decision can be reproduced from immutable rows rather than re-derived.

---

## 2. Why survivorship bias is structurally impossible here

The universe tables retain every market ever discovered — active, closed,
resolved, cancelled and invalid. Nothing is deleted for being uninteresting.
A market that stops appearing in discovery is *flagged*, never removed:

```python
def prune_stale_market_flags(session, *, older_than_hours: int = 48) -> int:
    """They are never deleted — that would reintroduce survivorship bias."""
```

So "all markets that existed at time T" is a query, not a reconstruction.

The corollary for operators: **do not delete historical rows to save disk.**
`OPERATIONS.md` gives the correct levers (sampling interval, change threshold,
liquidity floor) and says so explicitly.

---

## 3. Required design for the runner

### Walk-forward, never a single split

```
|<-- train -->|<-- validation -->|<-- out-of-sample -->|
              |<-- train ------->|<-- validation ---->|<-- OOS -->|
                                 |<-- train ------->|<-- val -->|<-- OOS -->|
```

Fitting on the whole history and then reporting performance on that same history
is not evidence. Each window fits on data strictly before its validation period,
which fits strictly before its out-of-sample period.

### Purging and embargo

Prediction-market observations are not independent. Two markets in the same
neg-risk group resolve together; a market predicted daily for a month yields
thirty correlated rows.

The runner must therefore:

* **Purge** training observations whose resolution falls inside the validation
  window. Otherwise the model trains on an outcome it is about to be tested on.
* **Embargo** a period after each validation window before training resumes, so
  serially-correlated observations do not leak across the boundary.
* **Group by event**, so all legs of a neg-risk group land in the same fold. A
  group split across folds leaks the answer, because the legs sum to 1.

### Outcome selection

Only resolved markets with `is_ambiguous = false` count. An ambiguous
resolution is excluded rather than guessed — scoring against a guessed outcome
would corrupt every metric that depends on it.

Predictions made *after* the resolution became known are excluded too. Without
that filter, a prediction generated during the settlement window scores as a
brilliant call, which is look-ahead arriving through the back door. This filter
is already implemented in the metrics worker:

```python
.where(Prediction.predicted_at < Resolution.known_at)
```

and the phase-1 gate goes further, requiring a 24-hour margin.

### Execution must be simulated, never assumed

A backtest that marks a position at the signal price is a fiction. The runner
must reuse `execution/paper.py`, which walks the recorded book, applies real
spread and depth, models latency, and permits partial fills. That code exists and
is tested; the runner should call it rather than reimplement it.

---

## 4. What must be reported

Reporting only aggregate return is how a broken strategy looks good.

**Calibration first**, because a miscalibrated profitable model is lucky:
Brier score, log loss, reliability diagram, ECE and max calibration error.

**Skill versus the market**, always. Brier skill score against the
market-implied probability at prediction time. The market is well calibrated
too; the only question that matters is whether we improved on it. Already
implemented in `engines/calibration.py`:

```python
def skill_versus_baseline(model_predictions, baseline_predictions, outcomes) -> dict:
```

**Financial results net of modelled costs**: P&L, ROI, win rate, average win and
loss, expectancy, profit factor, maximum drawdown and its duration, and
recovery time.

**Sliced**, not just aggregate: by category, model version, confidence bucket,
edge bucket, liquidity bucket and time horizon. An aggregate that looks fine
while one category carries all the loss is a result worth knowing.

**Sample size, always.** Any metric computed on fewer than 20 resolved
observations must report `insufficient_data` rather than a number. This is
already enforced:

```python
MIN_SAMPLE_FOR_SCORE = 20
```

**Never annualised returns** on a short history. Extrapolating six weeks to a
year is arithmetic dressed up as a claim, and the spec forbids it.

---

## 5. Data sufficiency

Backtesting cannot begin before the data exists.

| Requirement | Threshold | Why |
|---|---|---|
| Resolved markets with a prediction ≥24h prior | 500 (`MIN_TRAINING_OBSERVATIONS`) | Below this, walk-forward folds are too small to mean anything |
| Distinct events represented | ~100 | Correlated legs inflate apparent sample size; events are the real unit |
| Continuous snapshot coverage | ≥30 days | Fill simulation needs a book at each decision point |
| Categories represented | ≥3 | Otherwise the result is about one category, not the model |

On a fresh install none of these is met, and the model-health page says so
directly rather than showing an empty chart. Readiness is reported **per
category** as well as globally (`training_readiness_by_category`), and the two
are never pooled: 500 resolved election markets do not make a crypto model
ready to train. `MIN_CATEGORY_TRAINING_OBSERVATIONS` (default 150) is the
per-category bar.

---

## 6. Things that would invalidate a result

Written down so they are checked rather than remembered.

* Fitting adjustment magnitudes on the same data used to report performance.
* Choosing a filter threshold after seeing which threshold performed best.
* Reporting the best of several model variants without correcting for selection.
* Excluding "anomalous" markets after the fact.
* Simulating fills against the book at signal time when a later book exists.
* Counting an ambiguous resolution as whichever outcome the model predicted.
* Treating legs of one neg-risk group as independent observations.
* Reporting calibration without reporting skill against the market.

---

## 7. Honest expectation

The baseline currently has no external evidence and, by construction, mostly
reproduces the market price. The most likely backtest result is **a Brier skill
score near zero** — the model neither beating nor losing to the market by a
meaningful margin.

That is not a failure of the platform. It is the platform correctly reporting
that a model with no independent information has no independent edge. The
purpose of building the measurement apparatus first is to be able to tell the
difference between that and a real edge, rather than mistaking noise for one.
