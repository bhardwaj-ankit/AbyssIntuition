# September 17 laptop transfer

## Final source shutdown check: September 18, 01:15 Dubai

At the user's final close-out request, process inspection found no remaining
project Python/CLI workers, transfer Robocopy process, verification script, or
continuity monitor. Nothing remained to terminate. All five `AbyssIntuition-`
scheduled tasks remained Disabled. The source data is preserved; do not
restart collection on this laptop.

The completed SSD package contains code commit
`b09def195cd3a82dc950ae5bff413df617e386b1`. Its verified Git bundle SHA-256 is
`569e0d5834167819dfb571ef2f4acb8802b18a4764808fb5b40b50f183536229`;
the completed transfer manifest SHA-256 is
`fd34e4ccc1e992ec4c7f161b1b6c7491006b6e098f7180b284b2eb64252ddde1`.
The package was no longer accessible at its former `E:` path during this final
check. This subsequent shutdown documentation is pushed to GitHub but is not
in that already-completed bundle. A destination with this later documentation
must verify the bundle against its recorded commit, then inspect the later
documentation-only changes; the payload and its recorded hashes are unchanged.

## Verified transfer result: September 18, 01:12 Dubai

The frozen source and SSD payload match exactly: **135,374 files,
20,111,080,896 bytes**, zero missing or extra files, zero size differences,
and zero SHA-256 differences. All seven production databases passed fresh
`quick_check` on both source and SSD. The independent retained ZIP verification
also passed checksums and integrity for all seven restored databases.

Full comparison completed at `2026-09-17T21:12:11.409651Z`.
The `payload_files.json` SHA-256 is
`1639f6fe91e4bcb6688980e47befc001eb728780e4704e00ca64a825bd670563`.
Per-database hashes are in `database_verification.json`; the manifest records
the final code commit and bundle hash. Use the package's `READ_FIRST.txt` and
`VERIFY_TRANSFER.py` to verify the media and the new NTFS copy. The original SSD
transfer and all source data remain preserved.

This completes preparation of the portable transfer, not activation of the
new laptop. Source collection is stopped and disabled. Destination ownership
begins only after its own read-only preflight, environment/test checks, exact
NTFS copy validation, and duplicate-worker checks pass. No new model was
trained or promoted.

## Full collection ownership handoff authorized September 18

The user explicitly chose to move collection to the new laptop. All five
source tasks were disabled and stopped at `2026-09-17T20:25:08.7003739Z`
(September 18 00:25:08 Dubai). Liquidation Python PID 22800 and market Python
PID 8520, their launchers, and their wrappers are no longer running. Source
task XML and before/after process evidence are in the package's `cutover_audit`.
Do not re-enable this laptop's tasks after moving the SSD.

Both configured local Docker engine pipes were absent; no Docker backend or
project Python worker remained. Docker's Windows helper service alone was
running. `docker compose ps --all` could not load the missing `.env.docker`;
it did not establish a running project deployment.

Latest hourly recovery completed at 20:04:44 UTC: 10/10 new CryptoHFT partitions,
zero failures, 175 processed events, advancing the archive cutoff to
`2026-09-17T19:00:00Z` exclusive. Five new snapshots and 25 labels advanced the
active cohort to September 17 16:01 UTC. The live continuity monitor completed
with `passed: true` before shutdown. These are source cutoffs, not claims about
the destination or the completeness of unpublished daily archives.

Implementation and preparation commits `02a0818` and `4f5bc76` were pushed;
the branch was verified against GitHub before shutdown. The final bundle and
manifest identify the later documentation commit. Transfer verification is
authoritative only when the package manifest says `status: complete`; this
document does not independently certify a running copy operation.

## Preparation record

Frozen training counts are 10,723 snapshots and 53,615 labels for each of the
five active coins. Retired PEPE retains 10,658 snapshots and 53,290 labels;
totals including that historical evidence are 64,273 snapshots and 321,365
labels. `live_market.db` contains 3,456 raw observations. Per-symbol/family
counts and cutoffs are in `cutover_audit/data_cutoffs.json`.

The user requested moving the entire data estate to another laptop that will
also be used for model training, and committing the project changes.
Implementation commit `02a081868e12bcb8e99b594b9a5fc578db185392`
passed all 127 tests. A verified complete-history Git
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

The source initially continued collecting while the user's ownership choice
was pending. The user has now selected a full handoff as recorded above. The
two modes were distinguished as follows:

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

After every preflight and destination-copy check passes, a full-handoff
destination can activate its new virtual environment and run:

```powershell
.\deploy\windows\install_data_tasks.ps1 -DataRoot $DataRoot -DailyRefreshTime "04:30" -StartCollector
```

`$DataRoot` must be the discovered, verified NTFS path. This starts the live
liquidation collector, live market worker, and recent-history catch-up and
schedules the daily refresh and watchdog. Confirm exactly one of each worker,
ten live liquidation streams, fresh market observations, completed catch-up,
and the 30-minute continuity gate. Keep the source laptop disabled. Audit and
recover the interval after the recorded cutoff; hourly archive publication
lags remain expected. The installer uses destination local time for 04:30;
report its timezone. These interactive tasks require the user to stay logged in.

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
