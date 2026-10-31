# torr-arr-importer v1.4.1 — MINI-Ubuntu production validation

Date: 2026-10-31  
Repository branch: `feature/in-run-get-verification-v1.4.1`  
Pull request: [#6 — draft](https://github.com/evgendman/arr2torr/pull/6)

## Result

v1.4.1 was installed on MINI-Ubuntu and both production profiles completed a manual normal run successfully. The production SQLite database passed integrity checks before and after the in-place schema migration and after both profile runs. The PR remains a draft; no tag or GitHub Release was created.

## Installed source identity and backup

- Previous runtime: v1.4.0; installed-file Git blob SHA `d7b0bc6a84b45ffee9cfd190b18f0a4a66c617d7`.
- Installed runtime: v1.4.1; Git blob SHA `c9943d92a396882765a92e01cb40d28aa7388777`, matching the candidate source.
- Installed path: `/opt/torr-arr-importer/importer.py`; ownership/mode: `root:root 0755`.
- v1.4.0 backup: `/tmp/torr-arr-importer-v1.4.0-backup-20261031-213932`.
- Before migration, SQLite backup was created with SQLite backup support at `/tmp/torr-arr-importer-v1.4.1-pre-migration-20261031-214522/state.db`.
- Backup SHA-256: `c701a4a5a953f12b59a25b183a54bf1963538fb0126b398fe9200f3661ab6dda`.
- Backup `PRAGMA integrity_check`: `ok`; initial row counts: imports=728, pending_adds=22, terminal_events=1.

## In-place migration

The first v1.4.1 StateDB initialization added only the missing `pending_adds.missing_observations` column. No tables or existing rows were reset.

Post-migration checks:
- `missing_observations` exists.
- `PRAGMA integrity_check`: `ok`.
- imports=728, pending_adds=22, terminal_events=1.

## Dry-run checks

Both profiles completed a dry-run with exit code 0 and no TorrServer requests.

| Metric | Radarr 1080p | Sonarr 1080p |
|---|---:|---:|
| Arr history records | 662 | 1,596 |
| Grabbed records | 655 | 1,590 |
| Unique hashes | 582 | 169 |
| Duplicate Grab records | 73 | 1,421 |
| Dry-run candidates | 1 | 0 |
| Duration | 1.90 s | 5.68 s |
| Exit code | 0 | 0 |

The sole Radarr dry-run candidate was Groundhog Day (1993), TMDb 137 / IMDb tt0107048, hash `933b69e6356324d46f23d5dabadb238317c6d1cc`.

A read-only re-arm audit found no newer Arr Grab for any existing terminal-failure row: Radarr 0/7, Sonarr 0/15. All 22 rows remained terminal with attempt_count=3.

## Manual production run: Radarr 1080p

- Arr history: records=662, grabbed=655, unique_hashes=582, duplicates=73.
- Groundhog Day initially returned TorrServer status `Torrent in db` without usable FileStats.
- v1.4.1 queued the exact hash and polled using the shared in-run `action=get` carousel.
- About three seconds later, the exact hash returned usable FileStats; log outcome: `ADDED ... verification=usable-file-stats recovery=action-get-in-run`.
- Summary: submitted=0, added=1, failed=0, pending=22, duration=4.03 s.
- No duplicate `action=add` was submitted.

Post-Radarr SQLite check:
- imports=729.
- Groundhog Day hash appears once in `imports`.
- Groundhog Day hash no longer appears in `pending_adds`.
- pending_adds total remains 22.

Trace: `/tmp/torr-arr-importer-v1.4.1-radarr-live-20261031-215942.log`.

## Manual production run: Sonarr 1080p

- Arr history: records=1,596, grabbed=1,590, unique_hashes=169, duplicates=1,421.
- Summary: submitted=0, added=0, failed=0, pending=22, duration=5.31 s.
- Seerr summary: candidates=16, requests=0, added=0, already_present=0, errors=0.
- Exit code=0.

Trace: `/tmp/torr-arr-importer-v1.4.1-sonarr-live-20261031-220128.log`.

## Final SQLite and service state

After both manual runs:
- `PRAGMA integrity_check`: `ok`.
- imports=729.
- pending_adds=22: Radarr failed=7, Sonarr failed=15.
- Groundhog Day hash appears in imports exactly once.

The terminal failure records were preserved. The pre-run read-only status audit showed previously completed native blocklist, Seerr Watchlist and notification outcomes; the manual runs did not generate new Seerr requests or failures.

Both importer services and both 1080p timers were observed inactive after the manual runs; no importer process remained. The operator was separately given the following command to enable and start only the 1080p timers:

```bash
sudo systemctl enable --now \
  torr-arr-importer@radarr-1080.timer \
  torr-arr-importer@sonarr-1080.timer
```

Timer activation is not claimed as complete in this record until `systemctl status` / `systemctl list-timers` confirms both timers active and scheduled. The 4K timers were not touched.

## Remaining work and publication state

- Representative CPU/request-count measurements under controlled idle, pending, small-batch and larger-library workloads remain outstanding.
- A v1.3.5 executable A/B comparison remains unavailable: no public v1.3.5 tag exists, and the only local backup directory is root-private; its permissions were not changed.
- The v1.4.1 source is installed and manually live-validated, but the GitHub PR remains draft. No tag or release was published.
- This functional validation does not by itself establish downstream torr2strm/Jellyfin ingestion or playback.
