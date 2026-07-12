# Project Brief & Solution Intent: Fine-Tuned Signal + Price Model

## 1. Purpose

Re-architect AbyssIntuition's core intelligence away from a hand-tuned heuristic
engine and toward a fine-tuned model that learns directly from historical
market data. The model should produce, for a given symbol and horizon:

- a **directional signal** (`LONG` / `SHORT` / `FLAT`) with confidence, and
- a **predicted price target/range** for that horizon,

both learned end-to-end from historical price action, order-flow, and
positioning data, rather than assembled from rule-based scoring.

## 2. Background

The current engine (`SignalEngine` + feature modules under
`src/liquidity_signal/features/`) computes direction and confidence from
weighted heuristics over liquidity imbalance, trend regime, positioning, and
liquidation structure. That pipeline is deterministic, explainable, and
already ships with:

- historical + live Binance data ingestion (`data/binance_client.py`)
- a resolved-label training store (`runtime/liquidation_history.db`,
  triple-barrier outcomes)
- dataset export tooling (`export-lora`, `prepare-mlx-lora`)
- LoRA fine-tuning + tabular ML experiments (`ai/local_lora_trainer.py`,
  `ai/tabular_trainer.py`)
- a fail-closed deployment gate (`ai/deployment.py`) that keeps unproven
  models shadow-only

Recent experiments (SmolLM2-135M LoRA classifier, ExtraTrees "signal
validator") trained on this data have **not yet cleared the deployment gate**
on any symbol/horizon combination tried — directional precision has stayed in
the 0.30–0.44 range against a 0.52 bar. That result is the reason to rethink
the modeling approach rather than keep tuning the same shape of model.

## 3. Problem Statement

The heuristic engine encodes the designer's assumptions about which features
matter and how they combine. It does not learn from outcomes. A model that is
fine-tuned on historical (features, resolved outcome) pairs can in principle
discover nonlinear and regime-dependent relationships the heuristics miss —
but only if the data, targets, and evaluation are structured to avoid
lookahead bias and overfitting, which is where the prior experiments were
weakest (e.g., mimicking the old engine's own decision fields).

## 4. Goals

1. Replace (or run in parallel with) the heuristic scoring layer with a model
   trained on historical snapshots to predict both signal and price.
2. Keep the existing deterministic risk/execution layer (TP/SL sizing, Bybit
   order placement, hard risk limits) untouched — the model informs the
   decision, it does not execute trades directly.
3. Produce a model whose held-out, walk-forward performance clears an
   explicit, pre-registered bar before it can leave shadow mode.
4. Reuse existing data/export/gating infrastructure wherever it already
   fits, instead of building a parallel pipeline.

## 5. Non-Goals

- Do not ask the model to output order size, leverage, or liquidation-level
  math — that stays deterministic in the engine.
- Do not optimize for backtested PnL directly during model selection; PnL
  backtesting is a separate, later gate on top of classification/regression
  quality.
- Do not pursue this for symbols/horizons with insufficient resolved history
  to support a chronological train/validation/test split.

## 6. Solution Intent

### 6.1 Data

- Snapshot unit: one resolved (features → outcome) row per symbol per
  horizon, as already produced by the backfill + export pipeline.
- Features: engineered market state (trend/regime, liquidity imbalance,
  positioning, funding/basis, liquidation map) at decision time — price
  levels converted to relative distances (bps from close), not absolute
  values, so the model generalizes across price regimes.
- Labels: two targets per row —
  - **direction label**: resolved LONG/SHORT/FLAT from the existing
    triple-barrier logic.
  - **price target**: realized price (or return) at horizon close, for
    regression.
- Splits: chronological, by snapshot time, never randomized — matches the
  existing `train/validation/test` split convention already used in exports.
- Exclude any feature derived from the old engine's own decision (the prior
  experiments found the model learning to mimic `decision_action`,
  `took_trade`, etc. instead of the market — those fields must stay excluded).

### 6.2 Model Shape

**Predictor = gradient-boosted trees, not an LLM.** For structured
market-microstructure tabular data at this scale (~10^4 rows, ~10^2 features),
gradient-boosted decision trees are the honest state of the art and are far more
sample-efficient than fine-tuning a language model. This is not a preference —
it is what the evidence shows: a SmolLM2-135M LoRA classifier on this exact data
scored 35.9% on a 3-class test set (≈ random), because 10^4 structured rows
teach an LLM the output *format*, not market edge. The implementation lives in
`ai/gbdt_trainer.py` and uses `sklearn.HistGradientBoosting`, which is
LightGBM-equivalent, already installed (no new dependency), and — critically —
handles missing values natively (see §6.5).

Two heads over one shared feature matrix:

- **Classification head** (`HistGradientBoostingClassifier`): LONG/SHORT/FLAT
  barrier label, `class_weight="balanced"` (crypto snapshots skew FLAT-heavy).
  A decision policy (probability threshold + margin) is tuned on the validation
  split; below it, the model abstains to FLAT.
- **Regression head** (three `HistGradientBoostingRegressor` quantile models,
  q10/q50/q90): predicted forward return in basis points. q50 is the point
  estimate; [q10, q90] is reported as a price range around current price, so the
  output carries its own uncertainty instead of a bare number.

- Train one model per horizon; the loader augments every row with cross-asset
  BTC context (§6.5) so a single cross-symbol model can exploit BTC-beta.
- **The LLM is optional and secondary.** If natural-language trade rationale is
  wanted, a QLoRA (4-bit) fine-tune of Qwen2.5-7B-Instruct or Llama-3.1-8B fits
  comfortably in 16 GB VRAM and *narrates* the GBDT's numeric output. It never
  produces the prediction. Do not fine-tune sub-1B models (too weak) or attempt
  a full fine-tune of a 7B (won't fit in 16 GB).

### 6.3 Training & Evaluation

- Reuse `export-lora` / `prepare-mlx-lora` / `train-tabular-reviewer`-style
  tooling, extended with a regression target alongside the classification
  target.
- Walk-forward evaluation (train window sizes like the existing 14d/30d/45d
  experiments) instead of a single static split, to check stability over
  time rather than one lucky split.
- Evaluate classification and regression separately, then combined:
  - classification: balanced accuracy, macro F1, directional precision at
    confidence thresholds, per-class recall.
  - regression: MAE/RMSE of predicted vs. realized return, calibration of
    predicted range vs. realized outcome frequency.
- Reuse the existing fail-closed deployment gate pattern
  (`ai/deployment.py`) and extend its thresholds to also cover the
  regression target (e.g., realized outcome falls within predicted range at
  a minimum hit rate) before any candidate can flip from `shadow_only` to
  `live`.

### 6.5 Feature engineering, missingness, and cross-asset context

Implemented in `ai/gbdt_trainer.py`:

- **Leakage exclusion is explicit and enforced by a test.** Every flattened
  feature whose name matches an engine-decision prefix (`scoring.*`, `risk.*`,
  `bot_signal.*`, `cumulative.aggregate*`, `signal_direction/confidence/quality`,
  `decision_action`, `took_trade`, `liquidation_map.dominant_pull`,
  `data_quality_context.*`) is dropped. `test_gbdt_trainer.py` asserts none of
  them survive extraction. This is the single change that separates "model
  quality" from "imitating a bad signal".
- **Absolute prices → relative distances.** EMA/VWAP/support/resistance levels
  become signed bps-from-close, so the model generalizes across price regimes
  instead of memorizing price magnitudes.
- **Missing ≠ zero.** Derivatives that were unavailable at capture time
  (`oi_change_pct`, taker/global/top-trader ratios) are emitted as `NaN`, and
  `HistGradientBoosting` branches on missingness natively. A `0.0` from an empty
  data slice is a *lie* the previous pipeline told the model; `NaN` is the truth.
  Numeric columns with too few observed values to bin (the ~99%-missing
  derivatives on >30-day history) are pruned automatically.
- **Cross-asset BTC context.** Because alts are largely BTC-beta at short
  horizons, each row is augmented with contemporaneous BTC momentum/volatility/
  bias and the symbol's move *relative* to BTC (`xasset.*`). This is the most
  promising *new-signal* lever once positioning data is available.
- **Cyclical session encoding** (`hour_sin`/`hour_cos`) so 23:00 and 00:00 are
  neighbours, plus session/weekday categoricals.

### 6.4 Rollout

1. **Shadow**: model runs alongside the heuristic engine, logging both
   outputs; no execution impact.
2. **Comparison**: measure agreement/disagreement with the heuristic engine
   and, where they disagree, which one the resolved outcome favored.
3. **Gated live**: only after clearing the extended deployment gate on a
   held-out chronological test window, with the strategy-level PnL backtest
   (fees, slippage, drawdown) as an additional final gate — matching the
   existing `strategy_gate_passed` placeholder already present in the
   deployment manifest.
4. Keep the heuristic engine as a fallback/veto layer during an initial live
   period rather than a hard cutover.

## 7. Data Readiness (measured, not assumed)

Numbers below are from `runtime/training_v5.db`, the richest current store.

### 7.1 Volume and coverage

| Dimension | Value |
|---|---|
| Resolved snapshots | 12,936 (6 symbols × 2,156) |
| Time span | 89.8 days, **hourly** cadence |
| Horizons labelled | 5 / 15 / 30 / 60 / 240 min |
| Per symbol/horizon after purged 70/15/15 split | ~9,050 train / ~1,940 val / ~1,940 test (pooled across symbols) |

Verdict: **enough to *train* a GBDT, not enough to *trust* one.** ~13k pooled
rows is workable for gradient-boosted trees, but 90 days spans essentially one
market regime, so a single split cannot demonstrate regime robustness.

### 7.2 The binding data defect: 30-day derivative retention

Binance's `/futures/data/*` endpoints (open interest, taker ratio, global/top
long-short ratios) retain only ~30 days. On the 90-day set this makes the
single most predictive perp signal family almost entirely absent:

| Signal | Rows populated |
|---|---|
| `open_interest_change_pct` (nonzero) | **94 / 12,936 (0.7%)** |
| taker buy/sell ratio | 100 / 12,936 |
| funding rate | 9,548 / 12,936 (74%) |
| kline-derived features (momentum, vol, structure, regime, session) | ~100% |

This is **not a code bug** — the *REST* client is correct; that endpoint simply
stops serving data past 30 days. **Resolved:** the same metrics are published as
daily bulk CSVs on Binance Vision going back years, now ingested via
`data/binance_vision.py` / `ingest-vision-metrics` into `runtime/vision_metrics.db`
and joined into the trainer with `train-gbdt --vision-db`. After ingesting the
full 90-day span the derivative columns populate across the split (feature count
95 → 101; 60m directional precision 0.318 → 0.357). See
[DATA_INGESTION.md](../DATA_INGESTION.md) for the full platform inventory and
the daily/weekly jobs. The remaining lever is therefore **history span**, not
missing OI: 90 days is one regime.

### 7.3 Empirical ceiling (why this matters)

Running the new leakage-free two-head GBDT on `training_v5.db` (honest, purged,
chronological test split):

| Horizon | Directional precision | Sign hit rate | Range coverage | Gate |
|---|---|---|---|---|
| 15m | 0.419 | 0.507 | 0.690 | ❌ |
| 60m | 0.318 | 0.502 | 0.667 | ❌ |
| 240m | 0.404 | 0.505 | 0.595 | ❌ |

Every horizon fails, and sign-hit-rate sits at a coin flip (~0.50). Combined
with the finding that the *old engine's* directional precision is **0.393 vs a
0.517 majority baseline** — i.e. anti-predictive, and worse at higher confidence
— the conclusion is unambiguous and now proven with a correct instrument:

> The ceiling is **data, not model**. A properly built model finds no edge
> because the edge-bearing signals (OI/positioning) are missing and the history
> covers one regime. This is the honest state, and the fail-closed gate
> correctly refuses to deploy any of it.

### 7.4 Path to "enough good data"

1. **Backfill positioning from Binance Vision** (done): OI/taker/long-short
   ratios for the full history via `ingest-vision-metrics`, joined with
   `train-gbdt --vision-db`. This removes the missing-OI defect.
2. **Extend history span across regimes.** The binding constraint is now that 90
   days ≈ one regime. Ingest a longer Vision range and keep live capture running
   toward 6–12 months, then re-check the gate walk-forward.
3. **Fix OI at capture** (done for go-forward: `_open_interest_change_pct` is now
   multi-window / noise-robust).
4. **Add the highest-value missing signal**: liquidation-cluster distances (the
   map currently keeps only a scalar confidence, discarding cluster levels) —
   integrate Coinglass per [DATA_INGESTION.md](../DATA_INGESTION.md). Then
   multi-window CVD and funding × time-to-settlement.
5. **Re-evaluate walk-forward** across ≥2 non-overlapping windows before trusting
   any gate pass.

## 8. Recommended local stack (RTX 5080 16 GB / 64 GB RAM)

- **Predictor**: `sklearn.HistGradientBoosting` two-head (this repo,
  `ai/gbdt_trainer.py`). CPU-only, trains in seconds, zero VRAM. This is the
  right tool and the hardware is over-provisioned for it.
- **Optional narration LLM**: Qwen2.5-7B-Instruct or Llama-3.1-8B via **QLoRA
  4-bit** (~10–12 GB VRAM at batch 1 + grad-accum) — explains the GBDT output in
  language; never predicts. 16 GB is comfortable for this.
- **Do not**: fine-tune sub-1B models as predictors (proven ≈ random here), or
  attempt full fine-tunes of 7B+ (won't fit in 16 GB).

## 9. Risks

- **Overfitting to noise**: crypto short-horizon moves are low-signal;
  guard with walk-forward validation and conservative gate thresholds, not
  just a single held-out split.
- **Label leakage**: any feature that encodes information only available
  after the decision point (or that encodes the old engine's own choice)
  must be excluded — this was the main flaw found in the current
  experiments.
- **Regime shift**: a model trained on one volatility regime may not
  transfer; walk-forward windows and per-regime evaluation should be
  reported, not just an aggregate metric.
- **Two-headed objective conflict**: the regression and classification
  targets may pull the shared encoder in different directions; evaluate
  jointly-trained vs. separately-trained heads before committing to one.

## 10. Success Criteria

The re-architected model is ready to leave shadow mode only when, on a
chronological held-out test window it has never seen during training or
threshold selection:

- directional precision and coverage clear the same bar the current gate
  already enforces (or a revised, explicitly documented bar), **and**
- the predicted price range contains the realized outcome at a stated
  minimum hit rate, **and**
- performance is stable across at least two non-overlapping walk-forward
  windows, not just one.
