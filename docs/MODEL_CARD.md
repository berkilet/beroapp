# MODEL_CARD.md

## Model: `v0.1.0-baseline`

**Type:** interpretable, deterministic log-odds adjustment against the market prior
**Training data:** none — this model is not fitted
**Status:** active, and the only active estimator
**Location:** `backend/app/engines/probability.py`

---

## What it is for

Producing a probability for a binary Polymarket outcome that is *independent
enough* to be worth comparing against the market, while being honest about how
little it independently knows.

## What it is not for

Beating the market. There is no evidence that it does, no measurement showing
that it does, and on a fresh deployment there is good reason to expect that it
does not. See **Known limitations**.

---

## Design stance

A liquid prediction market is a strong forecast. A model that disagrees loudly
with one on no evidence is not insightful, it is broken. So the baseline **agrees
with the market by default** and departs from it only for a specific, nameable
reason, with the size of the departure scaled by how much it actually knows.

Concretely: it converts the market midpoint to log-odds, applies at most three
named adjustments, converts back, and then shrinks the result toward the market
in proportion to its own uncertainty. At maximum uncertainty the output *is* the
market — which is the correct answer for a model that knows nothing.

---

## Inputs

| Feature | Source | Notes |
|---|---|---|
| `market_midpoint` | CLOB order book | (best bid + best ask) / 2, from the raw book, not from Gamma metadata |
| `executable_price` | CLOB order book | Price from walking the book for the reference size |
| `spread_pct` | CLOB order book | Spread relative to midpoint |
| `book_imbalance` | CLOB order book | (bid − ask) notional / total notional |
| `hours_to_resolution` | Gamma `endDate` | |
| `negrisk_group_sum` | CLOB, aggregated | Sum of sibling YES midpoints in a neg-risk group |
| `negrisk_group_size` | Gamma | Number of legs in that group |
| `snapshot_count` | internal | How many observations exist for this market |

Every input is filtered on `known_at <= as_of`. There is **no text input**: no
market description, resolution text, or news reaches this model. That is the
structural reason prompt injection cannot move a probability.

---

## The three adjustments

### 1. Neg-risk coherence — the only genuinely model-free signal

Polymarket groups mutually-exclusive outcomes (`negRisk`). Their YES prices must
sum to 1. When the observed sum is S ≠ 1, at least one leg is mispriced and the
direction is known: if the group sums to 1.06, every leg is on average 6% too
expensive. Each leg is revised toward `p / S`, capped at ±0.60 in log-odds.

Two guards, both learned from testing against live data:

* **Group coverage ≥ 98%.** A 128-leg group of which we have priced six sums to
  near zero. Treating that as a 94% coherence error would manufacture an enormous
  edge out of our own incomplete sampling. Under-covered groups are dropped
  entirely and the model falls back to agreeing with the market.
* **Deviation ≤ 0.20.** A genuine 30% arbitrage across a liquid group would not
  sit waiting for us. Far more likely we are missing legs or pricing a group
  mid-reshuffle, so the adjustment is dropped rather than acted on.

### 2. Book imbalance

Resting depth is a weak short-horizon predictor of drift. Capped at ±0.15 in
log-odds, compressed with `tanh` (a very lopsided book is usually one large
resting order rather than information), and decayed with a one-week e-folding so
it contributes nothing to an event months away.

### 3. Favourite–longshot bias

Longshots are systematically overpriced relative to realised frequency; heavy
favourites slightly underpriced. One of the most replicated findings in the
prediction-market literature. Applied only in groups of ≥4 outcomes and only
outside [0.10, 0.90], capped at ±0.25 in log-odds, and **symmetric about 0.5** so
it can never become a one-directional bet.

---

## Uncertainty and confidence

`uncertainty` starts at 0.90 — near-total — and is reduced only by observable
data quality:

| Condition | Effect |
|---|---|
| Neg-risk coherence constraint available | −0.35 (the largest reduction available, because it is a genuinely independent constraint) |
| Spread < 2% of midpoint | −0.10 |
| Spread > 15% of midpoint | +0.05 |
| ≥ 30 snapshots observed | −0.05 |
| < 24 hours to resolution | +0.10 (the market has absorbed what is knowable; our latency disadvantage is worst here) |

Shrinkage toward the market is exactly this uncertainty. It is the mechanism
that stops a model with no evidence from producing a large edge, and on a fresh
install it means the model reports the market price with an edge of zero. That
is the intended behaviour, not a failure.

`confidence` is confidence in the *estimate*, not in the outcome. It is not
`1 − uncertainty`: the model can be confident it has correctly spotted a small
coherence error while remaining very uncertain about the event. It is forced to
0 when there is no two-sided market.

---

## Output validation

Any output that is non-numeric, NaN, infinite, outside [0,1], missing a required
field, or tagged with an unregistered model version is **rejected**. It is
recorded as a `model_rejection` system event and no prediction is stored. It is
never replaced with a default or a random value.

---

## Evaluation

**None yet.** `performance_summary` on this model version is explicitly `null`,
and the model-health page says so.

Evaluation requires resolved markets that were predicted at least 24 hours in
advance. The metrics worker computes Brier score, log loss, calibration curve,
ECE and a Brier skill score against the market-implied baseline, sliced by
category, model version, confidence bucket, liquidity bucket and time horizon —
but it reports `insufficient_data` until the sample supports a figure, and the
dashboard shows that rather than a number.

The comparison that matters is **skill versus the market**, not calibration
alone. The market is well calibrated too; being well calibrated is not an
achievement, it is the entry fee.

---

## Known limitations

Stated in full, because a model card listing only strengths is marketing.

1. **It has no external evidence.** No evidence connector is implemented. Its
   only independent input is a coherence constraint derived from Polymarket's
   own prices. It is closer to a market-microstructure filter than a forecasting
   model, and it should be read that way.

2. **Neg-risk coherence may not be exploitable.** The sum-to-one violation it
   detects is real, but capturing it generally requires trading several legs
   simultaneously. Buying one leg of an incoherent group is a directional bet
   with a coherence flavour, not an arbitrage. The platform does not currently
   construct multi-leg positions.

3. **The adjustment magnitudes are chosen, not fitted.** ±0.60, ±0.15, ±0.25 in
   log-odds are defensible starting points from the literature and from
   reasoning about the mechanism. They are not optimised, and they have not been
   validated out of sample. Fitting them would require the resolved-market
   history the system does not yet have.

4. **Favourite–longshot correction is a population effect applied per market.**
   The bias is well documented in aggregate. Applying it to an individual market
   assumes that market is typical of the population, which it may not be.

5. **It is worst where it matters most.** Near expiry, where prices move most,
   uncertainty is raised and the model defers further to the market — correctly,
   because our data latency is a real disadvantage there, but it means the model
   contributes least exactly when the market is most active.

6. **It cannot read resolution criteria.** Resolution ambiguity is scored by
   crude text heuristics (length, presence of words like "official"), not by
   understanding. A market that resolves against us on wording is a failure mode
   this model cannot see.

7. **No category-specific models exist.** The architecture supports them and the
   schema records a category per model version, but every market currently gets
   the same estimator regardless of category.

8. **Sports are excluded by policy**, not because they are unmodelable. They are
   dominated by specialist models with data this platform does not have.

---

## When this model should be retired

* When enough markets have resolved to fit and validate a calibrated estimator
  on a walk-forward basis (default threshold: 500).
* If measured calibration shows systematic bias that the adjustments cannot
  explain.
* If the neg-risk coherence signal proves non-exploitable in paper trading — in
  which case the baseline has no independent content at all, and should say so
  rather than continue producing near-zero edges that look like output.

A production model is never silently replaced. Every version is registered with
its feature set, hyperparameters, training and validation periods, and its
measured performance, and the previous version is retired with a timestamp
rather than overwritten.
