# Liquidation API White Paper

## 1. Purpose of This Document

This document explains the liquidation API implemented in this repository as a functional system, not as source code.

It is written so that:

- product teams can understand what the API is trying to represent
- frontend teams can render it correctly
- downstream services can consume it safely
- data teams can interpret each field without reading the implementation

The focus is the liquidation platform exposed by these endpoints:

- `GET /liquidation-map`
- `GET /liquidation/events`
- `GET /liquidation/tiles`
- `GET /liquidation/replay`

This is an implementation white paper for the current system, not a generic market-structure research note.

---

## 2. What This API Is Trying to Solve

The API is designed to answer one practical question:

> Where is leveraged futures liquidity likely concentrated around the current market price, and how is that view changing over time?

In crypto perpetual futures, liquidation pressure matters because forced closing of over-leveraged positions can accelerate price moves. The system therefore tries to expose two things together:

1. A modeled liquidation surface
   This is an estimate of where liquidation pressure is likely concentrated, even when no actual liquidation event has happened yet.

2. A real liquidation event layer
   This is a stream of actual forced-liquidation events coming from exchange feeds.

The API combines both into a single contract so that consumers can answer:

- where liquidity is likely sitting now
- whether the map currently leans toward upside squeeze risk or downside flush risk
- whether the view is purely modeled or supported by real liquidation prints
- how the estimated surface has evolved over recent time windows

The output is meant to be chartable, machine-readable, and replayable.

---

## 3. System Objective

The liquidation subsystem has five objectives.

### 3.1 Estimate hidden liquidation pressure

Exchanges do not publish a full book of every trader’s entry price and leverage. Because of that, the system infers likely liquidation concentration from public derivatives data.

### 3.2 Incorporate real liquidation events

When real liquidation streams are available, the model overlays them onto the estimated surface so that the output becomes more grounded in current market behavior.

### 3.3 Produce a price-by-time heatmap contract

The system is intentionally built so frontend or external services can render a CoinGlass-style graph:

- X-axis: time
- Y-axis: price
- color: estimated liquidity concentration

### 3.4 Persist history

The system stores snapshots, events, and downsampled tiles so consumers can:

- replay prior states
- request chart-friendly tiles
- calibrate present estimates using historical realized liquidations

### 3.5 Expose quality and degradation clearly

The API explicitly tells consumers whether they are seeing:

- model-only output
- model plus live-event overlay
- degraded mode because streams are missing or stale

This is important because the liquidation map is an estimate, not an exchange-certified truth source.

---

## 4. High-Level Architecture

At a high level, the liquidation platform has four layers.

### 4.1 Ingestion layer

The system consumes public market data from Binance and live liquidation streams from Binance and Bybit.

### 4.2 Modeling layer

The system transforms public derivatives data into an estimated liquidation surface across price levels.

### 4.3 Runtime and storage layer

The system keeps recent normalized live events in memory and also persists them to SQLite along with heatmap snapshots and prebuilt tiles.

### 4.4 Serving layer

FastAPI returns the current map, recent events, latest tile for a resolution, or historical replay snapshots.

---

## 5. Data Consumed by the Liquidation API

The liquidation API is not based on one endpoint. It consumes multiple public datasets because liquidation risk is not directly observable.

## 5.1 Binance REST data used for the model

For each symbol, the system consumes the following Binance USD-M futures datasets:

| Dataset | Why it is used |
| --- | --- |
| Mark price and index price | Current reference price and funding context |
| Order book depth | Local liquidity context and depth imbalance |
| Open interest history | Position buildup and leverage participation |
| Recent klines | Price path, volatility, range location, and volume distribution |
| Funding rate history | Long/short crowding proxy |
| Basis history | Perp-vs-index premium or discount |
| Taker buy/sell volume ratio | Aggressive flow bias |
| Global long/short account ratio | Broad account-level positioning bias |
| Top trader long/short account ratio | Higher-quality crowding proxy |
| Top trader long/short position ratio | Position-size crowding proxy |

These inputs are all public and are treated as observable proxies for hidden leverage concentration.

## 5.2 Live liquidation event feeds

The system also consumes real liquidation events from:

- Binance futures websocket `forceOrder`
- Bybit public linear websocket `allLiquidation.{symbol}`

These are normalized into a shared event schema so the API can serve one consistent event list regardless of source.

## 5.3 Stored historical data

The system also consumes its own persisted history from SQLite:

- previously stored liquidation events
- previously stored liquidation-map snapshots
- previously stored heatmap tiles

This historical layer is used for:

- replay
- warm-starting the runtime buffer
- calibration of model intensity and long/short bias

---

## 6. Data Normalization

Before modeling, the system normalizes incoming data into common internal forms.

## 6.1 Liquidation event normalization

Every live liquidation event is converted into a common object with:

- `source`
- `symbol`
- `liquidated_side`
- `execution_side`
- `price`
- `quantity`
- `notional`
- `timestamp`
- `received_at`
- `exchange_event_id`

### Meaning of `liquidated_side`

`liquidated_side` means the side that got forced out:

- `LONG` means long positions were liquidated
- `SHORT` means short positions were liquidated

This is not the same as the execution side used by the exchange stream.

### Why this normalization matters

Without normalization, downstream systems would need exchange-specific logic for:

- side interpretation
- quantity and price field names
- timestamp fields
- deduplication logic

The liquidation API hides that complexity.

## 6.2 Time alignment

Historical model inputs such as funding, OI, basis, and ratios do not always arrive on the same timestamps as candles. The system therefore aligns them by choosing the closest or latest applicable row for a given modeling timestamp.

This makes the heatmap history internally consistent across different data cadences.

---

## 7. Core Modeling Idea

The liquidation map is an inferred surface, not a direct feed.

The model answers:

> If current public derivatives data reflects how leveraged positioning is distributed, where would long and short liquidation pressure most likely sit across nearby prices?

The system builds that answer in stages.

## 7.1 Build a price grid

The first step is to create a price ladder around the current mark price.

- `range_pct` controls how far above and below the current price the map should extend
- `resolution` controls how many price bins exist in that range

Example:

- current mark price: `100,000`
- `range_pct=10`
- `resolution=48`

The map spans:

- low bound: `90,000`
- high bound: `110,000`
- 48 evenly spaced price levels

This ladder becomes the Y-axis for the heatmap and the basis for all level calculations.

## 7.2 Infer where traders likely entered

The system does not know every actual trader entry. It therefore builds synthetic long-entry and short-entry profiles from recent candles.

For each recent candle, it uses:

- candle range
- close location
- candle volume
- candle recency
- candle direction

Recent candles are weighted more heavily than older ones. Volume also increases influence. A candle’s price span distributes weight across the price bins it touched.

The result is two normalized profiles:

- estimated long entry distribution across price bins
- estimated short entry distribution across price bins

This is the model’s answer to:

> If recent trading activity represents current cohort buildup, where did those cohorts likely accumulate?

## 7.3 Infer how crowded longs vs shorts are

The system next decides how much of total positioning pressure should be assigned to long liquidation risk versus short liquidation risk.

It uses these observable factors:

- recent price return
- open interest change
- average recent funding rate
- basis
- taker buy/sell ratio
- global long/short ratio
- top trader account ratio
- top trader position ratio

These are combined into a crowding score.

Interpretation:

- positive crowding score means long-side positioning appears more crowded
- negative crowding score means short-side positioning appears more crowded

The score is then passed through a sigmoid transform to create:

- `long_share`
- `short_share`

Those shares determine how much estimated liquidation intensity is assigned to long-side versus short-side surface construction.

## 7.4 Infer leverage distribution

The system assumes leveraged positioning is distributed across leverage buckets:

- `5x`
- `10x`
- `25x`
- `50x`
- `100x`

These are not treated equally.

The model dynamically reweights leverage buckets based on:

- short-term volatility
- crowding bias

General behavior:

- lower volatility and stronger crowding allow more weight to high leverage
- higher volatility reduces high-leverage emphasis

This matters because high-leverage cohorts liquidate much closer to market price than low-leverage cohorts.

## 7.5 Convert entry profiles into liquidation prices

For each leverage bucket, the model estimates liquidation distance using:

- an approximate leverage-based liquidation percentage
- a fixed maintenance margin assumption

The current implementation uses:

- leverage-derived distance from `1 / leverage`
- maintenance margin rate of `0.5%`

For each hypothetical entry price bin:

- long positions map to liquidation prices below entry
- short positions map to liquidation prices above entry

Each mapped liquidation price is assigned intensity based on:

- entry profile weight
- inferred side crowding
- leverage-bucket weight
- distance decay
- closeness to current market price
- open interest scale
- historical calibration multiplier

This produces:

- `long_surface`
- `short_surface`
- `total_surface`

Each is a vector over the same price ladder.

## 7.6 Apply notional and activity scaling

The map is not purely shape-based. It also scales by market participation.

Two important scaling components are used:

- notional scale from total open interest value
- activity scale from change in open interest

Interpretation:

- larger open interest makes the surface larger
- stronger recent OI change increases crowding influence

This prevents the map from treating a quiet market and a highly leveraged market as equally important.

---

## 8. Historical Calibration

The system does not leave the model static. It calibrates current intensity using historical outcomes stored in SQLite.

## 8.1 What calibration tries to do

Calibration asks:

> When the model previously predicted intensity near a price, and real liquidation events later happened near that price, was the model systematically too weak or too strong?

## 8.2 How calibration is computed

For historical events in the lookback window:

1. The system finds the nearest earlier stored snapshot.
2. It finds the nearest price bin to the realized liquidation event.
3. It compares realized notional to predicted intensity.
4. It aggregates those comparisons into:
   - `intensity_multiplier`
   - `long_bias_multiplier`
   - `short_bias_multiplier`

### Meaning of the calibration outputs

- `intensity_multiplier`
  Global scaling factor for modeled intensity

- `long_bias_multiplier`
  Adjustment factor applied to long-liquidation crowding

- `short_bias_multiplier`
  Adjustment factor applied to short-liquidation crowding

- `historical_event_count`
  Number of persisted historical events used as calibration evidence

Calibration is intentionally bounded so the model cannot explode from a small sample.

---

## 9. Real Event Overlay

The modeled surface exists even if no live liquidation event arrives. But when live events do exist, the system overlays them onto the map.

## 9.1 Why overlay exists

The estimated surface answers where pressure likely is.

The live overlay answers where forced unwinds are actually happening now.

Combining both produces a more actionable map than either one alone.

## 9.2 How overlay works

When recent liquidation events are available:

- each event is mapped to the nearest price bin
- event notional is scaled relative to the predicted surface
- intensity is added not only to the center bin, but also to neighboring bins

This creates a localized bump rather than a single-point spike.

## 9.3 Event weighting

The system does not let live events fully replace the modeled surface.

Instead, it computes an `event_weight` based on coverage:

- more recent events increase the overlay weight
- overlay weight is capped

This is important because liquidation streams are real but incomplete. A pure event-only map would understate latent risk whenever there have simply been no recent forced prints.

## 9.4 When overlay is considered active

Overlay is active only when:

- events were requested
- recent normalized events exist
- event weight is greater than zero

This state is exposed directly in `quality.event_overlay_active`.

---

## 10. Derived Outputs and Why They Exist

The API does not return just one blob. It returns several derived views of the same model because different consumers need different levels of abstraction.

## 10.1 `levels_above` and `levels_below`

These are the strongest estimated liquidation levels above and below current price.

Interpretation:

- `levels_above` are primarily short-liquidation concentrations
- `levels_below` are primarily long-liquidation concentrations

Each level contains:

- `side`
- `leverage`
- `level_price`
- `distance_bps`
- `strength`

Why this exists:

- simple bots can use top levels without consuming the full heatmap
- alerting systems can watch a small set of high-risk levels

## 10.2 `clusters_above` and `clusters_below`

Nearby levels are grouped into clusters to reduce noise.

Each cluster contains:

- center price
- min and max price
- total strength
- contributing level count

Why this exists:

- cluster-level signals are easier to display and reason about than isolated bins
- a cluster often better represents a practical squeeze zone than one exact price

## 10.3 `heatmap_price_levels`

This is the canonical Y-axis of the heatmap.

Every intensity vector in the response aligns positionally with this array.

This means:

- `heatmap_price_levels[i]` is the price
- `heatmap[n].total_intensity[i]` is the total liquidity estimate at that price for time slice `n`

Consumers should treat this array as authoritative.

## 10.4 `heatmap`

This is the full price-by-time surface.

Each slice contains:

- `timestamp`
- `current_price`
- `total_intensity`
- `long_liquidation_intensity`
- `short_liquidation_intensity`

Why this exists:

- it is the main charting contract
- it supports CoinGlass-style heatmap rendering
- it lets systems distinguish long-side and short-side pressure separately

## 10.5 `depth_bands`

These are cumulative order-book depth points near the current price.

Why they exist:

- liquidation pressure alone is not enough
- a chart consumer often wants to compare liquidation concentration with nearby visible resting liquidity

## 10.6 `market_metrics`

This section exposes the main observable inputs used to build the map.

It includes:

- mark price
- index price
- basis
- funding
- open interest value and change
- taker buy/sell ratio
- global and top-trader positioning ratios
- order-book imbalance
- recent volume
- realized volatility
- next funding time

Why this exists:

- it makes the response self-explanatory
- downstream systems can explain the map without separately fetching upstream data
- it gives integrators a health and context layer for trust decisions

## 10.7 `assumptions`

This section exposes internal modeling assumptions directly.

It includes:

- leverage buckets
- leverage weights
- maintenance margin rate
- clustering step
- range and resolution
- inferred long and short crowding

Why this exists:

- consumers can understand that the map is model-derived
- audit and analytics systems can track model state over time

## 10.8 `quality`

This is the most important section for external consumers.

It tells you whether the map should be trusted as:

- model-only
- model plus events
- degraded output

Key fields:

- `estimate_weight`
- `event_weight`
- `bybit_available`
- `event_overlay_active`
- `events_used`
- `degraded_mode`
- `source_age_ms`
- `degraded_reason`
- `stream_health`
- `notes`

If another system is consuming this API automatically, it should read `quality` before acting on the rest of the payload.

## 10.9 `events_summary`

This is a lightweight rollup of recent real liquidation events.

Use it when you want event context without reading the full `events` list.

## 10.10 `storage`

This section tells consumers how much persistent history exists.

It includes:

- event count in last 1 hour
- event count in last 24 hours
- replay snapshots available
- tile resolutions available

This is especially useful for dashboards that want to enable or disable replay and tile views based on actual stored data.

---

## 11. Determining Direction: `dominant_pull`

One key response field is `dominant_pull`.

This field is derived by comparing:

- total estimated upward liquidation magnetism
- total estimated downward liquidation magnetism

Implementation meaning:

- strong short-liquidation pressure above price contributes to upward pull
- strong long-liquidation pressure below price contributes to downward pull

Possible values:

- `LONG`
  The map suggests stronger upward squeeze potential

- `SHORT`
  The map suggests stronger downside flush potential

- `FLAT`
  The model does not see a meaningful directional imbalance

This is not a trade signal by itself. It is a directional interpretation of the liquidation surface.

---

## 12. Confidence

`confidence` is a normalized measure of directional dominance within the liquidation map.

It is derived from the imbalance between top pressure above and below current price.

Interpretation:

- higher confidence means one side of the map dominates more clearly
- lower confidence means the surface is balanced or noisy

Consumers should not treat confidence as statistical probability. It is a structural strength score for the current map.

---

## 13. Heatmap Contract for Other Systems

This section is critical for integration.

## 13.1 How to render the heatmap correctly

To build the main liquidation chart:

- X-axis = `heatmap[*].timestamp`
- Y-axis = `heatmap_price_levels`
- Z-axis = one of:
  - `heatmap[*].total_intensity`
  - `heatmap[*].long_liquidation_intensity`
  - `heatmap[*].short_liquidation_intensity`

Important shape rule:

- `heatmap` is an array of time slices
- each slice contains one intensity array per price level
- all intensity arrays align with `heatmap_price_levels` by index

## 13.2 How the existing UI uses it

The existing graph uses:

- price on the right-side Y-axis
- time on X-axis
- blue to green to yellow coloring
- hover text showing estimated liquidity at a given price and time

The graph should treat price as primary and color as quantity.

## 13.3 Recommended machine-consumption behavior

If another system needs the full current map:

- use `/liquidation-map`

If another system needs a chart-ready matrix and wants lower payload:

- use `/liquidation/tiles`

If another system wants actual recent events only:

- use `/liquidation/events`

If another system wants historical state snapshots:

- use `/liquidation/replay`

---

## 14. Endpoint-by-Endpoint Reference

## 14.1 `GET /liquidation-map`

This is the primary endpoint.

### Query parameters

| Parameter | Type | Meaning |
| --- | --- | --- |
| `symbol` | string | Futures symbol such as `BTCUSDT` |
| `include_events` | boolean | Whether to apply live event overlay |
| `event_limit` | integer | Max recent events to use and return |
| `range_pct` | float | Percent range above and below current price |
| `resolution` | integer | Number of price bins |
| `history_points` | integer | Number of time slices in returned heatmap |

### What it returns

A full modeled liquidation surface plus optional live-event overlay, market context, quality flags, recent events, and storage metadata.

### When to use it

Use this endpoint when you want:

- current map state
- all metadata required to interpret it
- top levels and clusters
- time-series heatmap slices

### Response interpretation summary

- `source="binance_model_only"` means no active event overlay was applied
- `source="binance_model+live_events"` means the model was overlaid with recent liquidation events

## 14.2 `GET /liquidation/events`

Returns recent normalized liquidation events.

### Query parameters

| Parameter | Type | Meaning |
| --- | --- | --- |
| `symbol` | string | Futures symbol |
| `limit` | integer | Max recent events |

### Behavior

The runtime in-memory buffer is preferred. If empty, the store is used as fallback.

### When to use it

Use this endpoint when:

- you need actual liquidation prints only
- you are building an event tape
- you want to augment your own chart independently of the model

## 14.3 `GET /liquidation/tiles`

Returns the latest stored heatmap tile for a symbol, range, and resolution.

### Query parameters

| Parameter | Type | Meaning |
| --- | --- | --- |
| `symbol` | string | Futures symbol |
| `resolution` | integer | Tile resolution |
| `range_pct` | float | Price range used for the tile |

### What it returns

- symbol
- generated timestamp
- range percent
- resolution
- time axis
- price levels
- total intensity matrix
- long intensity matrix
- short intensity matrix

### Why this endpoint exists

It is optimized for chart consumption and lower parsing overhead than the full liquidation-map payload.

## 14.4 `GET /liquidation/replay`

Returns previously persisted snapshot summaries.

### Query parameters

| Parameter | Type | Meaning |
| --- | --- | --- |
| `symbol` | string | Futures symbol |
| `limit` | integer | Number of historical snapshots |

### What it returns

Each replay row contains:

- symbol
- generated time
- current price
- dominant pull
- confidence
- source
- price-range bounds

### Why it exists

This is the lightweight historical replay API. It is useful for:

- timeline browsing
- QA
- historical comparison
- debugging chart state changes

---

## 15. Storage Design

The liquidation platform persists data in a local SQLite database under:

- `runtime/liquidation_history.db`

It uses WAL mode for better concurrent read/write behavior.

## 15.1 Stored tables

### `liquidation_events`

Stores normalized live liquidation events.

Purpose:

- persistence across restarts
- replay
- runtime warm boot
- calibration input

### `liquidation_snapshots`

Stores full `liquidation-map` payloads over time.

Purpose:

- replay
- historical comparisons
- calibration against later realized events

### `liquidation_tiles`

Stores chart-oriented tiles at multiple resolutions.

Purpose:

- quick frontend retrieval
- lower-payload rendering
- avoiding repeated transformation on every request

---

## 16. Runtime Behavior

## 16.1 Watchlist warm loop

On application startup, the system starts liquidation tracking for a default watchlist:

- `BTCUSDT`
- `ETHUSDT`
- `SOLUSDT`
- `BNBUSDT`

It periodically generates and persists fresh liquidation-map snapshots and tiles.

## 16.2 Live stream runtime

For each watched symbol, the runtime keeps source-specific streaming collectors for:

- Binance
- Bybit

For each source and symbol, it tracks:

- connectivity
- last message time
- last event time
- buffered event count
- reconnect count
- last error

These values are surfaced in `quality.stream_health`.

## 16.3 Runtime bootstrapping

When a symbol is first activated, the runtime preloads recent stored events into memory so the API is not empty immediately after process start.

---

## 17. Multi-Resolution Tile Strategy

The system stores more than one tile resolution because different consumers have different needs.

Typical use:

- lower resolutions for small widgets or overview charts
- higher resolutions for detailed desktop heatmaps

The tile endpoint exists so charting systems do not need to recalculate or downsample the full response themselves.

---

## 18. Quality and Degradation Rules

Consumers should explicitly handle three operating modes.

## 18.1 Mode A: Model-only

This means:

- the liquidation surface was produced from Binance-derived market inputs
- no live liquidation overlay was applied

Typical indicators:

- `source = "binance_model_only"`
- `quality.event_overlay_active = false`

## 18.2 Mode B: Model plus live events

This means:

- the estimated surface exists
- live Binance or Bybit liquidation events were applied as overlay

Typical indicators:

- `source = "binance_model+live_events"`
- `quality.event_overlay_active = true`

## 18.3 Mode C: Degraded

This means:

- event overlay was requested
- but streams were unavailable or stale enough that only reduced functionality was possible

Typical fields to inspect:

- `quality.degraded_mode`
- `quality.degraded_reason`
- `quality.source_age_ms`
- `quality.stream_health`

### Recommended downstream rule

If you are building automation, first check:

1. `quality.degraded_mode`
2. `quality.event_overlay_active`
3. `quality.stream_health`

Do not assume that a non-empty response always means full live-event coverage.

---

## 19. Example Consumption Patterns

## 19.1 Frontend heatmap chart

Use:

- `/liquidation/tiles` for the heatmap matrix
- `/liquidation-map` for labels, clusters, quality, and metrics

## 19.2 Alerting system

Use:

- `/liquidation-map`

Watch:

- `dominant_pull`
- `confidence`
- strongest `levels_above` or `levels_below`
- `quality.event_overlay_active`

## 19.3 Research pipeline

Use:

- `/liquidation/replay`
- `/liquidation/events`

This combination lets you compare:

- prior map states
- later realized liquidation activity

## 19.4 Bot or signal filter

Use:

- `/liquidation-map`

Potential logic:

- require `confidence` above threshold
- require non-degraded quality
- use `dominant_pull` as confirmation or veto
- downweight decisions when `quality.event_overlay_active=false`

---

## 20. What Other Systems Should Assume

To consume this API safely, other systems should assume the following.

### 20.1 The map is modeled, not absolute truth

The system infers liquidation pressure from public data. It does not know private positions.

### 20.2 Real events are real but incomplete

Live liquidation feeds add grounding, but they are not a complete cross-market record of all forced liquidations everywhere.

### 20.3 Price index alignment matters

Consumers should use the response’s own `current_price`, `price_range_low`, `price_range_high`, and `heatmap_price_levels` rather than recomputing them externally.

### 20.4 Quality fields are first-class

Any downstream automation should inspect `quality` before acting.

### 20.5 Intensity is comparative

`strength` and heatmap intensity are best interpreted comparatively:

- stronger vs weaker zones
- denser vs thinner areas
- increasing vs fading pressure over time

They should not be treated as guaranteed executable notional resting on an exchange book.

---

## 21. Practical Meaning of Key Fields

| Field | Practical meaning |
| --- | --- |
| `dominant_pull` | Which side of the map is structurally stronger |
| `confidence` | How dominant that side is inside the current map |
| `levels_above` | Strongest short-liquidation zones above price |
| `levels_below` | Strongest long-liquidation zones below price |
| `heatmap_price_levels` | Canonical price axis |
| `heatmap[].total_intensity` | Total estimated liquidation pressure at each price for that time slice |
| `heatmap[].long_liquidation_intensity` | Estimated long-liquidation pressure by price |
| `heatmap[].short_liquidation_intensity` | Estimated short-liquidation pressure by price |
| `market_metrics` | Observable market inputs used by the model |
| `assumptions` | Modeling choices and inferred internal state |
| `quality` | Whether the map is fully live, model-only, or degraded |
| `events` | Full recent normalized liquidation events |
| `events_summary` | Lightweight event rollup |
| `storage` | Available persistent history and tile coverage |

---

## 22. Known Limits of the Current Implementation

This section is intentionally explicit so integrators do not over-assume.

### 22.1 It is not a direct liquidation ledger

The map is an inference layer built from public data proxies.

### 22.2 It is currently exchange-scoped, not market-universal

The model is driven primarily by Binance public market data, with live event overlay from Binance and Bybit.

### 22.3 Calibration depends on stored history quality

If the system has only a short history or sparse events, calibration will remain conservative.

### 22.4 Intensity units are model-scaled

The surface is designed to preserve comparative usefulness and chartability. Consumers should not assume it equals a guaranteed exchange-published liquidation notional book.

---

## 23. Recommended Integration Rules

If you are integrating this API into another system, the safest defaults are:

1. Use `/liquidation-map` as the source of truth for current interpretation.
2. Use `quality` before acting on `dominant_pull` or `confidence`.
3. Use `heatmap_price_levels` as the only price axis for heatmap arrays.
4. Use `/liquidation/tiles` when you only need chart matrices.
5. Use `/liquidation/events` when you need actual liquidation prints, not modeled structure.
6. Use `/liquidation/replay` for historical state reconstruction.
7. Treat intensity values as relative concentration, not exact guaranteed book liquidity.

---

## 24. Final Summary

The liquidation API is a structured market-intelligence service for leveraged futures.

It consumes:

- Binance public derivatives data for structural modeling
- Binance and Bybit liquidation streams for real-event confirmation
- stored historical snapshots and events for persistence and calibration

It transforms those inputs into:

- current directional liquidation structure
- top levels and clusters
- price-by-time heatmap matrices
- normalized event tapes
- replayable historical snapshots
- explicit quality and degradation metadata

Its objective is to let any consumer answer, in one place:

> Where is liquidation pressure likely concentrated, how strong is it, how has it evolved, and how much of the current map is supported by real liquidation activity?

That is the core contract of the liquidation API.
