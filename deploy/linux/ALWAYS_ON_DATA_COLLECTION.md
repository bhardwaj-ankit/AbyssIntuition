# Always-on free data collection

The live Binance and Bybit liquidation feeds cannot replay events missed while
the collector is offline. Run the collector on one machine that remains online
and treat its `runtime` directory as the source of truth.

## Host choices

- An existing Raspberry Pi, NAS, mini PC, or old laptop is the most dependable
  no-subscription option.
- For a UAE-based deployment, [Oracle Cloud has Dubai and Abu Dhabi
  regions](https://docs.oracle.com/en-us/iaas/Content/General/Concepts/regions.htm)
  and is the first hosted option to try if an Always Free shape is available.
  Oracle documents that [free capacity can be scarce and idle Always Free
  compute may be reclaimed](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm),
  so retain verified off-host backups.
- [Google Cloud Free Tier](https://docs.cloud.google.com/free/docs/free-cloud-features)
  `e2-micro` is an alternative, but its eligible compute regions and outbound
  allowance are restricted. Exchange connectivity must be smoke-tested from
  the selected cloud IP before it becomes authoritative.
- GitHub Actions is not a collector host. [Hosted jobs are limited to six
  hours](https://docs.github.com/en/enterprise-cloud@latest/actions/reference/limits),
  creating regular unrecoverable feed gaps even before scheduling delays.

No public HTTP port is needed for collection. Keep only SSH reachable and use
key authentication.

## Deploy on any Linux host with Docker

Install Git and Docker, clone this repository, then start only the two data
services:

```bash
git clone <your-repository-url> AbyssIntuition
cd AbyssIntuition
mkdir -p runtime
docker compose up -d collector daily-refresh
docker compose ps collector daily-refresh
docker compose logs --tail=100 collector daily-refresh
```

Leave the deployment in observation for at least 30 minutes. Do not stop the
existing collector until `data-operations-health` reports all 12 streams fresh
and at least 95% common-bucket continuity on the new host.

`collector` continuously records Binance and Bybit forced-liquidation events.
`daily-refresh` immediately refreshes replayable public sources, creates a
restore-verified backup, checks health, and repeats every 24 hours. Both use the
host-mounted `runtime` directory and restart after a process failure or host
reboot when Docker starts at boot. Model training is intentionally disabled on
the small collector host; train from the verified data on the capable machine.

Do not run a second collector against a different copy of the database and then
try to combine the SQLite files. Keep one authoritative host. Stop services
cleanly before moving the complete runtime state:

```bash
docker compose stop collector daily-refresh
```

The compressed archives in `runtime/backups` are suitable for off-host copies.
Verify one after download before relying on it:

```bash
liquidity-signal verify-data-backup runtime/backups/<archive-name>.tar.gz
```

## Operational checks

```bash
docker compose ps collector daily-refresh
docker compose logs --since=30m collector
docker compose exec daily-refresh liquidity-signal data-operations-health
```

A deliberate shutdown still stops collection. System sleep prevention on the
Windows workstation and container restart policies on Linux reduce accidental
gaps; they cannot record data while their host has no power or network.
