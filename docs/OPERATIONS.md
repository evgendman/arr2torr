# Operations and troubleshooting

## Understand one cycle

A normal cycle logs a concise summary of each stage:

1. START — selected instance, Arr type, category and target lane.
2. HISTORY — full history records, Grabbed records, unique hashes, duplicates and skipped invalid records.
3. A TorrServer list snapshot is read.
4. SNAPSHOT — number of distinct Arr grabs, hashes currently on TorrServer, hashes already known to the shared import history and new candidates.
5. For each candidate, Arr media/series details are fetched as needed, then an add is sent to TorrServer.
6. ADD_ACCEPTED logs TorrServer's initial acknowledgement; ADDED is logged only after readiness or database-presence evidence confirms the hash.
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

## Asynchronous add verification (v1.1.0)

An ADD_ACCEPTED log entry means TorrServer acknowledged the add request; it is not a completed import. ADDED is logged only after the importer observes readiness or explicit database-presence evidence for the exact hash.

The importer submits eligible candidates first, then polls one shared TorrServer action=list snapshot at a configurable interval. Each attempt has its own deadline measured from the add request. Defaults are a 120-second verification deadline and a 3-second polling interval. Compare the deadline with TorrentDisconnectTimeout in the installed TorrServer build.

The shared state database contains:
- imports: confirmed import history and the existing intentional-removal safeguard;
- pending_adds: in-flight, retry-waiting, or exhausted attempts. This table stores safe descriptive fields but not the original download URL or any API key.

After restart, pending attempts are checked against TorrServer before another request is sent. A present but not-yet-ready hash stays pending until the verification deadline. Confirmed attempts are atomically promoted into imports. Expired attempts use exponential backoff, capped at one hour; the default maximum is three add attempts. After that limit the attempt is left in failed state and normal polling does not automatically resend it.

To explicitly re-queue exhausted unverified attempts for one source instance, run:

    sudo -u torrimport /opt/torr-arr-importer/importer.py --config /etc/torr-arr-importer/radarr-1080.toml --retry-failed

Do not combine --retry-failed with --dry-run. This option re-queues only failed unverified attempts for the selected instance; it does not clear successful import history or automatically restore confirmed user-removed torrents. Use --rebuild only for intentional historical repopulation.

Verification means observable readiness (usable FileStats or working status) or explicit TorrServer database presence. It does not claim a durable disk-commit acknowledgement that TorrServer does not expose.

## API and authentication errors

- Arr API calls use X-Api-Key; check that each config contains the key for the correct Arr instance.
- Arr URLs must be base URLs; the importer appends /api/v3/history and /api/v3/movie/{id} or /api/v3/series/{id}.
- The TorrServer URL must point to the receiving server and its /torrents API must be reachable from the service account's host.
- A Prowlarr download URL may contain an API key in its query string. The importer redacts common API-key query parameters in logs and does not save the original downloadUrl into TorrServer metadata.
- HTTP 401/403 and other permanent 4xx failures are not retried repeatedly. HTTP 408, 429, 5xx and transient network failures are retryable.

## Apprise notifications (v1.2.0)

The importer sends only aggregated events through the shared Apprise API:

- `warnings` / `warning`: an updated numeric snapshot of attempts currently waiting for automatic retry. No title, release name, hash or media ID is included. Unchanged snapshots are suppressed across timer cycles.
- `critical` / `failure`: a grouped summary of newly terminal add/verification failures and permanent non-retryable HTTP 4xx preparation failures. It includes the human-readable title, release title when available, a short sanitized cause, add/verification attempts, and a best-effort Arr card link if `arr_ui_url` and `titleSlug` are available.

The shared `state.db` now also stores notification fingerprints and one-time terminal alert markers. Do not remove/reset it to troubleshoot notifications; doing so also erases importer history and deliberate-removal memory. Terminal alerts are marked attempted after one send attempt, even if Apprise is offline, so delivery failure does not create an endless alert loop.

Check local logs for delivery diagnostics:

    journalctl -u torr-arr-importer@radarr-1080.service -n 150 --no-pager
    grep -E 'APPRISE_(SENT|FAILED|SKIPPED)' /var/log/torr-arr-importer/radarr-1080.log

A valid configuration is opt-in and contains `notifications_enabled = true`, `apprise_url`, `apprise_config_key = "arr-stack"`, a stable `service_name`, a short timeout and the `critical`/`warnings` tags. Telegram credentials and topic IDs belong only in Apprise. If notification delivery fails, main import/retry state is still authoritative.

## Timers and overlapping runs

Every instance has an independent systemd timer and lock file. The default interval is five minutes. If the four instances are enabled together, their default timer settings may align; use per-instance timer overrides or enable them at staggered times to distribute API load.

If a service is running longer than expected:

    systemctl status torr-arr-importer@radarr-1080.service --no-pager
    journalctl -u torr-arr-importer@radarr-1080.service -n 200 --no-pager

A lock conflict indicates a run of the same configured instance is already active. Do not point different instances at the same explicit lock file; their default lock names differ by instance.

## What the importer will not fix

This service transfers the selected release and the safe metadata subset into TorrServer. It does not resolve a bad indexer result, configure Arr Quality Profiles or Custom Formats, manufacture missing IDs, generate NFO, or materialize the STRM tree. Those belong to the indexer/Arr configuration and downstream library-generation stages.

For the current field mapping and known gaps, see [Architecture](ARCHITECTURE.md) and [Roadmap](../ROADMAP.md).
