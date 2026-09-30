# Liquidation API Trade Signal Guide

## 1. Purpose

This document explains how the liquidation API can be used to improve trade signals.

It is not a description of how the liquidation API itself works. That is covered in [liquidation-api-white-paper.md](/Users/ankitbhardwaj/Documents/AbyssIntuition/liquidation-api-white-paper.md).

This guide answers a different question:

> How should another signal engine, bot, dashboard, or execution system use the liquidation API to make better trading decisions?

The goal is to help downstream systems use liquidation data as a practical trading input rather than as a visualization only.

---

## 2. What the Liquidation API Adds to a Signal System

Most signal systems already use some combination of:

- price action
- momentum
- volatility
- volume
- order book imbalance
- higher-timeframe trend

Those signals explain what the market is doing.

The liquidation API adds a different layer:

- where crowded leverage is likely concentrated
- which side of the market looks more vulnerable to forced unwinds
- whether real liquidation activity is starting to confirm that vulnerability

This matters because many strong crypto moves are not driven only by direction. They are amplified by forced position unwinds.

In practice, liquidation data improves signal systems in four ways:

1. Directional confirmation
2. Signal vetoing
3. Risk and target placement
4. Regime detection

---

## 3. Core Principle

The liquidation API should usually be treated as a second-order input, not the only source of truth.

That means:

- price and trend signals tell you what the market is doing now
- liquidation structure tells you how vulnerable that move is to acceleration or reversal

A good default rule is:

> Use liquidation data to confirm, strengthen, weaken, or reject another signal rather than replacing the primary signal engine entirely.

---

## 4. Best Ways to Improve Trade Signals

## 4.1 Use `dominant_pull` as a directional confirmation layer

The simplest use is directional confirmation.

Example:

- your base signal says `LONG`
- `/liquidation-map` returns `dominant_pull = LONG`
- `confidence` is high

Interpretation:

- the market structure suggests upside squeeze potential
- your directional trade has structural support from liquidation positioning

This can be used to:

- allow the trade
- increase confidence
- widen target expectation

The opposite case is just as useful:

- your base signal says `LONG`
- liquidation map says `SHORT`

Interpretation:

- the trade is fighting the current leverage structure
- the move may be less reliable

In that case, a system may:

- reject the signal
- reduce position size
- demand stronger confirmation before entry

## 4.2 Use liquidation as a veto in low-quality setups

Liquidation structure is especially useful when the base signal is weak or noisy.

Example:

- momentum signal is mildly bullish
- but `dominant_pull = SHORT`
- and `confidence` is strong

This is a classic veto case.

Why:

- a weak bullish setup is less trustworthy if liquidation pressure suggests downside flush risk

This is often more valuable than forcing liquidation data to generate entries by itself.

A strong signal engine usually improves more by rejecting bad trades than by adding more trades.

## 4.3 Use top levels and clusters as target and danger zones

The API returns:

- `levels_above`
- `levels_below`
- `clusters_above`
- `clusters_below`

These can be used to improve trade planning.

### For long trades

Useful interpretations:

- strong `clusters_above` can act as upside squeeze targets
- strong `clusters_below` can act as downside danger zones

### For short trades

Useful interpretations:

- strong `clusters_below` can act as downside flush targets
- strong `clusters_above` can act as invalidation danger zones

This helps a signal engine choose more realistic:

- take-profit levels
- stop placement zones
- partial exit levels

Instead of placing exits only from ATR or arbitrary fixed percentages, the system can align exits with likely liquidation magnets.

## 4.4 Use `quality` before trusting the liquidation signal

No signal engine should consume liquidation output blindly.

Before using the map, always inspect:

- `quality.degraded_mode`
- `quality.event_overlay_active`
- `quality.estimate_weight`
- `quality.event_weight`
- `quality.stream_health`

Practical rule:

- if `degraded_mode = true`, use liquidation only as a weak secondary input
- if `event_overlay_active = true`, the map deserves more trust
- if the map is model-only, treat it as structural context rather than live confirmation

This is the most important safety rule for automated systems.

---

## 5. Trade Signal Patterns That Benefit Most

Liquidation data does not improve every strategy equally.

It helps most when forced positioning matters.

## 5.1 Breakout strategies

Breakouts improve when the system asks:

- is there a large short liquidation cluster above current price?
- is there a large long liquidation cluster below current price?

Why this helps:

- a breakout into crowded opposing leverage can accelerate much faster than a normal breakout

Example:

- price is pressing resistance
- order flow is bullish
- `dominant_pull = LONG`
- large clusters sit above price

Interpretation:

- upside breakout has squeeze fuel

That makes the breakout more attractive.

## 5.2 Momentum continuation systems

Momentum systems often fail when the move is already exhausted.

Liquidation data helps separate:

- healthy continuation
- late, overextended continuation

Example:

- price is trending up
- momentum stays positive
- but long-liquidation pressure below price is much larger than upside short squeeze pressure

Interpretation:

- the market may be more vulnerable to a downside flush than a further squeeze

This can stop a continuation system from entering late.

## 5.3 Mean-reversion systems

Mean-reversion systems benefit when liquidation data shows that the move is near a heavy forced-unwind zone.

Example:

- price spikes into a large short-liquidation cluster above
- event overlay becomes active
- real short liquidations are printing

Interpretation:

- move may be near exhaustion
- reversal probability may rise after the squeeze completes

This does not mean immediate reversal is guaranteed. But it helps a mean-reversion system identify better exhaustion areas.

## 5.4 Scalping and intraday systems

Short-horizon systems benefit from liquidation data mainly as a filter.

For these systems, the best use is usually:

- direction confirmation
- invalidation tightening
- no-trade filtering when liquidation structure disagrees sharply

Scalpers should care a lot about:

- `quality.event_overlay_active`
- `events_summary`
- `source_age_ms`

Because intraday timing improves most when the liquidation layer is live, not stale.

---

## 6. Recommended Integration Models

There are several clean ways to integrate the liquidation API into an existing signal engine.

## 6.1 Confirmation model

Base signal produces:

- direction
- confidence

Liquidation map then modifies confidence.

Example logic:

- if base direction matches `dominant_pull`, increase confidence
- if it opposes `dominant_pull`, decrease confidence
- if liquidation confidence is very strong against the trade, reject the trade

This is the safest general-purpose design.

## 6.2 Veto model

Base signal remains unchanged unless liquidation structure strongly disagrees.

Example logic:

- ignore liquidation in neutral cases
- veto only when `dominant_pull` is opposite and liquidation `confidence` is above a high threshold

This is useful when you already trust the base model and want liquidation only as a protection layer.

## 6.3 Composite scoring model

Signal score can be built from multiple weighted components:

- trend score
- momentum score
- volatility score
- order flow score
- liquidation score

Example:

`final_score = 0.35 trend + 0.25 momentum + 0.20 flow + 0.20 liquidation`

This is powerful, but only if the system also respects `quality`.

Otherwise, stale or degraded liquidation data can pollute the composite score.

## 6.4 Regime-switching model

The liquidation API can determine when to switch strategy behavior.

Example:

- if large directional liquidation imbalance exists, favor breakout logic
- if map is flat and no events are active, favor mean-reversion or low-conviction logic

This is often more useful than directly mapping liquidation data to buy or sell calls.

---

## 7. How Specific Fields Improve Signals

## 7.1 `dominant_pull`

Use for:

- directional bias confirmation
- signal vetoing
- regime context

Interpretation:

- `LONG` means stronger upside squeeze structure
- `SHORT` means stronger downside flush structure
- `FLAT` means the map is balanced

## 7.2 `confidence`

Use for:

- deciding how strongly to trust `dominant_pull`
- scaling the weight of liquidation influence in a composite model

Interpretation:

- high confidence means the liquidation map has a strong directional imbalance
- low confidence means the map should have less influence on trading logic

## 7.3 `levels_above` and `levels_below`

Use for:

- target planning
- stop planning
- area-of-interest alerts

Interpretation:

- above levels represent likely short squeeze zones
- below levels represent likely long flush zones

## 7.4 `clusters_above` and `clusters_below`

Use for:

- defining broader zones instead of exact price triggers
- partial take-profit planning
- danger zone marking

These are usually more robust for trading decisions than raw individual levels.

## 7.5 `events_summary`

Use for:

- deciding if real liquidation activity is active enough to matter
- distinguishing quiet structural pressure from live cascading behavior

Useful signals include:

- rising event count
- rising total notional
- one-sided long or short liquidation dominance

## 7.6 `events`

Use for:

- live event tape logic
- event-driven alerts
- real-time dashboard overlays

This is especially useful for systems that want to react to actual liquidation bursts instead of only the modeled map.

## 7.7 `market_metrics`

Use for:

- context-aware signal adjustment
- explainability
- sanity checks

Example:

- if liquidation map is bullish but funding and top-trader ratios are already extremely crowded, a system may avoid chasing the move too late

## 7.8 `quality`

Use for:

- trust management
- fallback logic
- automation safety

For automated trading systems, `quality` should be treated as required input, not optional metadata.

---

## 8. Example Signal Improvement Rules

Below are practical rules another system can implement.

## 8.1 Long confirmation rule

Enter long only if:

- base model says `LONG`
- liquidation `dominant_pull = LONG`
- liquidation `confidence >= 0.60`
- `quality.degraded_mode = false`

Why:

- this filters longs to cases where liquidation structure supports upside extension

## 8.2 Short confirmation rule

Enter short only if:

- base model says `SHORT`
- liquidation `dominant_pull = SHORT`
- liquidation `confidence >= 0.60`
- `quality.degraded_mode = false`

## 8.3 Opposite-side veto rule

Reject any trade if:

- base direction opposes `dominant_pull`
- liquidation `confidence >= 0.70`

Why:

- strong liquidation structure against the trade often makes the setup lower quality

## 8.4 Aggressive breakout rule

Allow aggressive breakout only if:

- base breakout signal is active
- large cluster exists in the breakout direction
- `event_overlay_active = true` or recent event notional is elevated

Why:

- this identifies breakouts with squeeze fuel rather than thin breakouts

## 8.5 Size adjustment rule

Position size can be adjusted using liquidation agreement.

Example:

- full size when base signal and liquidation structure agree strongly
- half size when they agree weakly
- zero size when they disagree strongly

This is one of the safest ways to use liquidation data.

---

## 9. How It Improves Risk Management

Liquidation data should not only improve entries. It should also improve exits and risk.

## 9.1 Better stop placement

A stop placed directly inside a heavy liquidation zone may be too vulnerable.

A better approach is:

- identify nearby opposing-side cluster
- avoid placing stop where a cascade is likely to run through first

This reduces getting stopped inside obvious liquidation hunting areas.

## 9.2 Better take-profit planning

Strong liquidation clusters in the trade direction can be used as:

- first target
- final target
- partial-exit zone

This is often more market-aware than using only fixed R-multiples.

## 9.3 Better time-based risk decisions

If a trade is open and:

- `dominant_pull` flips
- live events start printing against the position
- `events_summary` becomes strongly one-sided

the system can reduce risk earlier rather than waiting for raw price invalidation.

---

## 10. How It Improves Signal Explainability

Another major benefit is explainability.

A signal engine using liquidation data can explain decisions in human terms such as:

- long bias confirmed by strong upside short-liquidation cluster
- short rejected because downside structure was weak and live events were inactive
- target widened because real short liquidations were already triggering above price

This makes the signal system easier to trust and easier to debug.

---

## 11. Recommended Consumption Priority

If another system wants to use this API well, the safest order of interpretation is:

1. Read `quality`
2. Read `dominant_pull` and `confidence`
3. Read `clusters_above` and `clusters_below`
4. Read `events_summary`
5. Use full `events` only if event-level logic is needed
6. Use `heatmap` when charting or calculating custom zone logic

This order prevents low-quality or degraded data from being overused.

---

## 12. When Liquidation Data Should Have Less Influence

The liquidation API should be downweighted when:

- `quality.degraded_mode = true`
- `confidence` is low
- `dominant_pull = FLAT`
- event overlay is inactive and the base strategy is highly short-term
- market is low participation and map intensity is weak

In these situations, liquidation structure is still useful as context, but should not strongly control signal decisions.

---

## 13. Common Mistakes to Avoid

## 13.1 Using liquidation as the only signal

The map is best used with price, trend, volatility, or order flow. On its own, it is more useful as structure than as a complete trade engine.

## 13.2 Ignoring `quality`

This is the biggest integration mistake. Model-only and live-overlay states should not be treated the same.

## 13.3 Treating intensity as exact executable size

Heatmap intensity is a modeled estimate of pressure concentration. It should be treated comparatively, not as an exact published liquidation ledger.

## 13.4 Overreacting to one event burst

Single liquidation bursts can mark continuation or exhaustion. They need to be interpreted in context with price action and the broader map.

---

## 14. Best Overall Integration Pattern

If a team wants one recommended design, the best default is:

1. Build a base signal from price, trend, and flow.
2. Use `/liquidation-map` as a confirmation and veto layer.
3. Use `clusters` for target and stop planning.
4. Use `quality` to adjust trust.
5. Use `events_summary` and `event_overlay_active` to detect live cascades.

This approach improves signals without making the strategy dependent on a single inferred dataset.

---

## 15. Final Summary

The liquidation API improves trade signals by adding structural information that most signal systems do not have:

- where crowded leverage likely sits
- which side is vulnerable to forced liquidation
- whether real liquidation events are actively confirming that risk

Used correctly, it helps systems:

- confirm or reject trades
- identify squeeze and flush setups
- improve target and stop placement
- size trades more intelligently
- detect when a move is structurally fragile

The best use is not to replace the base signal engine.

The best use is to make the base signal engine more selective, more explainable, and more aware of leverage-driven market behavior.
