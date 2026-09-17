# September 17 laptop transfer

## Current state: preparation, not a completed handoff

The user requested moving the entire data estate to another laptop that will
also be used for model training, and committing the project changes.
Implementation commit `02a081868e12bcb8e99b594b9a5fc578db185392`
passed all 127 tests. It has not been pushed. A verified complete-history Git
bundle is being prepared for offline transfer; the final manifest must record
the bundle's actual commit and SHA-256, including subsequent documentation.

Discovered source-machine paths, which must not be assumed on another laptop:

- Repository: `C:\Users\Ankit\Documents\demo temp\AbyssIntuition`.
- Active data: `D:\AbyssIntuitionData`, NTFS.
- New package: `E:\AbyssIntuitionTransfer-20260917`, exFAT transfer media.
- New payload: `E:\AbyssIntuitionTransfer-20260917\AbyssIntuitionData`.
- Original September 12 transfer: `E:\AbyssIntuitionData`, preserved untouched.

The preliminary live inventory contains 135,368 files and 20,087,768,013 bytes,
with no reparse points. These are not final snapshot counts. An initial
Robocopy pass copies the complete non-database tree, including caches, model
artifacts, exports, logs, audit evidence, and retained backup ZIPs. It excludes
`*.db`, `*.db-wal`, `*.db-shm`, and `*.db-journal`; therefore this pass alone
cannot produce a usable runtime. The package's `TRANSFER_MANIFEST.json`
currently says `preparing` and is deliberately not restore-ready.

The source continues collecting. The user's choice is pending between a full
ownership handoff and a training snapshot with collection staying here. Do not
stop source collection or install destination collectors until that choice is
recorded. The final manifest must distinguish the two modes:

- Full handoff: record source tasks disabled and workers stopped, then verify
  the frozen database estate and authorize one destination collection owner.
- Training copy: use consistent SQLite online backups on NTFS while source
  collection continues; record each snapshot cutoff, and prohibit destination
  collection task installation. Different databases can have different
  snapshot times; do not claim a simultaneous cross-database transaction.

## Final package requirements

Record a complete relative-path inventory with sizes and SHA-256 values,
database integrity checks, backup archive verification, Git bundle verification,
ownership mode, source task/process evidence, snapshot cutoffs, and any excluded
transient files. Compare against the frozen source or staged snapshot as
appropriate. Do not claim byte equality with an actively changing source.
Keep status `preparing` until every required comparison passes.

There are seven production databases: `liquidation_history.db`,
`training_v6.db`, `training_forward.db`, `vision_metrics.db`, `onchain_data.db`,
`cross_venue.db`, and `live_market.db`. Other database files in historical
audits and interrupted backup staging directories are retained evidence, not
replacement production databases. Interrupted staging files are not valid
backups. Never run writable databases on the exFAT SSD.

## Destination procedure

Read `NEW_LAPTOP_BOOTSTRAP.md` and its mandatory project context completely,
then use this newer transfer record and the completed manifest for current
state. Discover the actual attached SSD and package; do not pick the older
transfer because its manifest is already complete. Verify the new manifest,
bundle, payload hashes, seven databases, and backup archive. Inspect processes,
scheduled tasks, `docker ps --no-trunc`, and `docker compose ps --all` before
allowing any collection owner. Discover a confirmed empty NTFS destination
with adequate free space. Do not overwrite a nonempty directory.

Restore the repository from the verified bundle or matching remote commit,
install `.[ml,dev]` in a fresh environment, and run the complete tests. Copy
the verified payload to NTFS and repeat the full file/hash comparison and
database checks before use. Do not copy a virtual environment between laptops
or blindly install machine-specific task definitions. Keep TLS verification
enabled. Record actual paths, commit, counts, checks, and process IDs.

The older bootstrap's instruction to resume `cryptohft,market` is superseded:
full historical recovery completed. Audit any post-snapshot gap and use the
documented recent-history/daily refresh paths for the active five-symbol
universe. Install the five continuous/scheduled jobs only for an explicitly
verified full ownership handoff. A training-copy destination must not install
those jobs.

## Training constraints and retained evidence

Active symbols are BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, and NEARUSDT. Historical
PEPE rows remain as evidence but must be excluded from model training. Do not
run `ingest-cross-venue` or modify `cross_venue.db`. Its preserved raw SHA-256 is
`c1677a29bbf2d81d10049d79db73a0bdeb18c0cd565865cba45226843eb384be`.

The September 17 18:18 UTC five-symbol audit found 53,287 eligible 60-minute
rows, 149 features, and 444.08 effective days, passing the required coverage
thresholds and supporting eleven chronological 90/15/15-day windows. It
preceded the latest tail append. The subsequent catch-up added 64 snapshots
and 320 labels per active coin through September 17 15:01 UTC; hourly recovery
may extend these counts before transfer. Consult the final snapshot evidence.

Daily depth/on-chain archives have publication delays. New raw live market
observations are not automatically part of the archive training feature
contract. Rerun completeness and feature/label checks on the exact destination
snapshot, use chronological purged walk-forward evaluation, preserve missing
values, and check all deployment gates. Previous 180-day models failed
promotion. No new model has been trained or promoted, and transfer alone does
not authorize either action.
