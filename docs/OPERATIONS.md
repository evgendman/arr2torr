# Operations and troubleshooting

## Understand one cycle

A normal cycle logs a concise summary of each stage:

1. START — selected instance, Arr type, category and target lane.
2. HISTORY — full history records, Grabbed records, unique hashes, duplicates and skipped invalid records.
3. A TorrServer list snapshot is read.
4. SNAPSHOT — number of distinct Arr grabs, hashes currently on TorrServer, hashes already known to the shared import history and new candidates.
5. For each candidate, Arr media/series details are fetched as needed, then an add is sent to TorrServer.
6. ADDED confirms the response contained the expected hash.
7. DONE records candidates, added/failed counts, hashes skipped as already present, hashes skipped as previously imported, elapsed time and dry-run mode where applicable.

Example commands:

    journalctl -u torr-arr-importer@radarr-1080.service -n 150 --no-pager
    sudo -u torrimport /opt/torr-arr-importer/importer.py \
      --config /etc/torr-arr-importer/radarr-1080.toml --dry-run

If the timer is enabled, use systemctl list-timers 'torr-arr-importer@*' to check the next schedule.

## The three-set model

On a regular run, the importer compares:

- what Arr still reports as Grabbed (A);
- what exists in TorrServer right now (T);
- what the shared SQLite database remembers as previously imported (I).

The normal candidate set is:

    A − T − I

This provides two distinct protections:

- **TorrServer duplicate protection:** a hash already present in the receiving server is skipped, regardless of which Arr instance originally added it.
- **Deliberate-removal protection:** a hash once successfully imported, then removed from TorrServer by the user, remains in the shared SQLite history. Normal runs do not resurrect it.

This is intentional behavior, not a lost synchronization event.

## When to use --rebuild

--rebuild is an explicit recovery/repopulation mode. It ignores the historical exclusion set I for that execution, but still skips hashes currently present in TorrServer:

    Rebuild candidates = A − T

Use it when switching to a clean/new TorrServer database, or when you intentionally want to restore all source-history releases that are currently missing from TorrServer.

Example:

    sudo systemctl stop torr-arr-importer@radarr-1080.timer
    sudo -u torrimport /opt/torr-arr-importer/importer.py \
      --config /etc/torr-arr-importer/radarr-1080.toml --rebuild
    sudo systemctl start torr-arr-importer@radarr-1080.timer

Run rebuild for each relevant Arr instance if you want to repopulate the new TorrServer from all of their histories. Since all instances share SQLite state and each rebuild still checks current TorrServer membership, hashes added by the first instance will be seen by later instances and skipped.

**The database does not need to be deleted for rebuild.** Do not remove the shared state file just to refill a clean TorrServer. Clearing it erases intentional-removal memory for every instance.

## Why multiple releases of one movie/series are retained

Arr often focuses on the final chosen download in a conventional workflow. However, users can manually Grab different releases over time. Each unique infohash is treated as its own torrent. The importer deduplicates repeated history rows for the same hash, not distinct hashes for the same title.

That means different versions of one movie or series can coexist in TorrServer. The downstream library can expose each one as a distinct root with its own source identity and allow the viewer to choose.

## Why a release was not added

Use the HISTORY and SNAPSHOT log lines first.

| Log/result | Meaning and next check |
|---|---|
| skipped_no_data | The history record did not contain a usable data object. |
| skipped_no_hash | No data.torrentInfoHash; the importer cannot deduplicate it reliably. |
| skipped_no_url | No data.downloadUrl; the importer cannot pass the selected release to TorrServer. |
| skipped_present | The hash already exists in TorrServer. This is expected and safe. |
| skipped_known | The hash was previously imported, is now absent from TorrServer, and normal mode refuses to resurrect it. Use --rebuild only if restoration is intended. |
| FAILED | The candidate reached the add attempt but failed. Read the sanitized error and check API reachability, authorization and the TorrServer response. |
| candidates=0 | No eligible new hashes were found under the current filtering rule. It is not in itself an error. |

## API and authentication errors

- Arr API calls use X-Api-Key; check that each config contains the key for the correct Arr instance.
- Arr URLs must be base URLs; the importer appends /api/v3/history and /api/v3/movie/{id} or /api/v3/series/{id}.
- The TorrServer URL must point to the receiving server and its /torrents API must be reachable from the service account's host.
- A Prowlarr download URL may contain an API key in its query string. The importer redacts common API-key query parameters in logs and does not save the original downloadUrl into TorrServer metadata.
- HTTP 401/403 and other permanent 4xx failures are not retried repeatedly. HTTP 408, 429, 5xx and transient network failures are retryable.

## Timers and overlapping runs

Every instance has an independent systemd timer and lock file. The default interval is five minutes. If the four instances are enabled together, their default timer settings may align; use per-instance timer overrides or enable them at staggered times to distribute API load.

If a service is running longer than expected:

    systemctl status torr-arr-importer@radarr-1080.service --no-pager
    journalctl -u torr-arr-importer@radarr-1080.service -n 200 --no-pager

A lock conflict indicates a run of the same configured instance is already active. Do not point different instances at the same explicit lock file; their default lock names differ by instance.

## What the importer will not fix

This service transfers the selected release and the safe metadata subset into TorrServer. It does not resolve a bad indexer result, configure Arr Quality Profiles or Custom Formats, manufacture missing IDs, generate NFO, or materialize the STRM tree. Those belong to the indexer/Arr configuration and downstream library-generation stages.

For the current v1.0.3 field mapping and known gaps, see [Architecture](ARCHITECTURE.md) and [Roadmap](../ROADMAP.md).
