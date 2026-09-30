# Public liquidation data research — updated 2026-09-09

## 2026-09-09 recovery update

The earlier conclusion below was correct for the sources and access terms
verified on 2026-08-06, but it is no longer the final project status.
[CryptoHFTData](https://www.cryptohftdata.com/datasets/crypto-liquidation-data)
now exposes a free, no-key hourly Parquet archive for Binance Futures and
Bybit, including the workstation-shutdown interval. Its documentation states
that files are created only for symbol-hours containing liquidation events.

The archive was validated against an hour that the local collector observed
independently on 2026-09-09. All 40 Binance BTCUSDT rows and all 20 Bybit
BTCUSDT rows matched the local feed on trade time, side, price, quantity, and
the reconstructed exchange-compatible event identifier. This makes the source
suitable for incident recovery, while its own exchange pages still warn that
an independent archive cannot guarantee every exchange event.

`ingest-cryptohft-liquidations` now performs rate-limited, resumable recovery.
It caches every Parquet object, records its SHA-256 digest and hourly provenance,
records documented no-event hours explicitly, clips boundary hours to the exact
requested interval, and uses the same source/event IDs as live ingestion so
reruns cannot double-count events. It also maps both exchanges' remote
`1000PEPEUSDT` contract back to local `PEPEUSDT` units.

The shutdown recovery completed with 101,925 stored events across 4,596
audited partitions and zero failures. A separate repair recovered 4,026 Bybit
PEPE events for the entire period affected by the wrong live symbol alias.
Legacy pre-2026-08-19 objects were outer-Zstandard-wrapped; the importer now
detects and decompresses that format before validating the Parquet schema.

## Acceptance criteria

The missing ideal dataset is observed (not reconstructed) liquidation events
for BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, NEARUSDT, and PEPEUSDT, covering
2026-02-07 through 2026-08-05. It must include event time, side, price, size or
notional, auditable provenance, and a license compatible with local model
training. Empty periods must be distinguishable from collector downtime.

At the time of the original 2026-08-06 audit, no source found satisfied all of
those requirements for Binance USD-M.

## What was searched and validated

The audit covered GitHub repository search and file trees, Binance's public S3
index, Hugging Face, Kaggle, the Internet Archive CDX index, official exchange
and protocol documentation, and selected commercial catalogues for comparison.
This is a broad reproducible search of indexed public sources, not a claim that
every unindexed file on the internet was examined.

### Binance and GitHub

- [`binance/binance-public-data`](https://github.com/binance/binance-public-data)
  is the official archive downloader. The Binance S3 index has USD-M trades,
  metrics, depth and funding, but no USD-M `liquidationSnapshot` prefix.
- [`gcoban/binance-public-data-downloader`](https://github.com/gcoban/binance-public-data-downloader)
  makes the same archive matrix explicit: liquidation snapshots exist for
  COIN-M, not USD-M. The COIN-M archive does not reach the required 2026 period.
- [`lostleaf/binance-datatool`](https://github.com/lostleaf/binance-datatool)
  is a useful BSD-3 archive discovery/downloader, but cannot download an object
  family Binance does not publish.
- [`hyperliquid-dex/historical_data`](https://github.com/hyperliquid-dex/historical_data)
  contains real liquidations, but its CSV ends on 2023-05-01.
- Public collector projects such as
  [`Chennn0519/crypto-data-recorder`](https://github.com/Chennn0519/crypto-data-recorder)
  and [`raphael2025/market-data`](https://github.com/raphael2025/market-data)
  contain collection code, not the required archive.
- The reviewed liquidation-prediction repositories were rejected as data
  sources when they used price-drop proxy labels, commercial Velo data, or did
  not commit the underlying event data.

The Internet Archive CDX query returned no captures beneath Binance's expected
USD-M `liquidationSnapshot` prefix.

### Dataset hubs and other archives

- [`Chainticks/perp-data`](https://huggingface.co/datasets/Chainticks/perp-data)
  is the strongest usable result: CC-BY-4.0 observed Hyperliquid liquidation
  fills derived from official HyperCore S3, partitioned daily. The audited
  range is 2026-05-09 through 2026-08-04 with two absent dates. All six target
  assets are present; PEPE is represented by Hyperliquid's `kPEPE` contract.
- [`Chainticks/defi-liquidations`](https://huggingface.co/datasets/Chainticks/defi-liquidations)
  contains genuine Aave V3 Ethereum `LiquidationCall` events under CC-BY-4.0,
  but starts on 2026-05-10 and is broad DeFi stress context rather than perp
  liquidation flow.
- Kaggle candidates were rejected: one BTC sample covers January 2020 and says
  it is reconstructed; another 10,000-row report has no timestamp; another is
  explicitly synthetic.
- Hyperliquid's official node fill archive is comprehensive in principle, but
  its documentation marks S3 transfer as requester-pays and warns that archive
  data can be missing. It does not meet the requested free guarantee directly.
- At the time, CryptoHFTData and MoonDev were treated as commercial catalogues.
  CryptoHFTData's current documented free archive is the recovery source
  described in the update above.

## Implemented decision

`ingest-hyperliquid-liquidations` imports every published open partition as an
auxiliary observed cross-venue feature. It filters each matched liquidation to
the liquidated account's fill, normalizes sides and PEPE denomination, caches
the source Parquet, and writes per-day provenance records.

The Binance and Bybit WebSocket collector remains the primary source and the
only source of local continuity evidence. CryptoHFTData can recover observed
events after an outage, but it cannot recreate local heartbeats, snapshots, or
proof that the collector was connected. The completeness manifest therefore
continues to fail closed rather than relabeling an offline interval as locally
observed.

The completed import stored 451,256 filtered events across the six assets. A
60-minute shadow walk-forward ablation did not improve directional precision:
0.3027 with the archive versus 0.3100 without it. Regression sign-hit moved from
0.5057 to 0.5113, but every fold still failed the deployment gate. The source is
therefore retained as partial observed context, not evidence that training is
finished or that the model is production-ready.
