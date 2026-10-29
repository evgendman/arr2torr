# Operations and troubleshooting

## Understand one cycle

A normal cycle logs a concise summary of each stage:

1. START — selected instance, Arr type, category and target lane.
2. HISTORY — full history records, Grabbed records, unique hashes, duplicates and skipped invalid records.
3. A TorrServer list snapshot is read.
4. SNAPSHOT — number of distinct Arr grabs, hashes currently on TorrServer, hashes already known to the shared import history and new candidates.
5. For each candidate, Arr media/series details are fetched as needed, then an add is sent to TorrServer.
6. ADD_ACCEPTED logs TorrServer's initial acknowledgement; ADDED is logged only after the exact hash appears in TorrServer's action=list response.
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

## Radarr blocklisting after terminal TorrServer failure (implemented; live terminal-path validation pending)

radarr_blocklist_enabled defaults to true for Radarr. It can be disabled per Radarr config with radarr_blocklist_enabled = false; Sonarr profiles never execute this path.

The importer acts only on pending_adds rows whose TorrServer add/verification workflow reached status = 'failed' and whose recorded attempt_count is at least max_add_attempts. A permanent API rejection before that configured attempt limit is reached is not sufficient to blocklist the release. It calls Radarr's native POST /api/v3/history/failed/{arr_history_id} for that specific Grab history record. Radarr runs its normal failed-download pipeline to create the blocklist entry and can initiate a replacement search according to Radarr's settings. A retry_wait row is not blocklisted; preparation errors in terminal_events (for example, an expired download URL before the TorrServer add) are also excluded.

Look for these log events:

- RADARR_BLOCKLIST_BLOCKED: Radarr accepted the native failed-history action.
- RADARR_BLOCKLIST_ALREADY_PRESENT: after an ambiguous request, a blocklist entry with the same source release title was found.
- RADARR_BLOCKLIST_VERIFY_PENDING: the request outcome is ambiguous; the importer is performing read-only checks and does not resend the failed-history request.
- RADARR_BLOCKLIST_FAILED: identity was missing, Radarr rejected the operation, or verification remained inconclusive after three successful checks.
- RADARR_BLOCKLIST_SKIPPED: the setting is disabled, or a terminal row has fewer attempts than max_add_attempts. The latter reason is saved and explained in the terminal notification.

Radarr's public BlocklistResource currently does not expose the torrent infohash. Therefore, recovery after an ambiguous response can match sourceTitle for that movie, but cannot independently prove that the matched entry has the same hash. The normal path does not use a fake POST /blocklist endpoint; it relies on Radarr's own history/failed API.

The blocklist outcome is appended to the existing grouped terminal Apprise failure notification, not sent as a separate message. If Apprise is disabled, the result remains in the logs. The new state fields are migrated in-place in the shared SQLite database. Existing terminal failures found during the first migration are marked legacy_skipped to prevent retroactive mass blocklisting; new terminal failures are processed automatically. Deliberately re-arming an exhausted attempt with --retry-failed resets its blocklist state.

**Live-validation caveat:** success is defined by exact hash membership in a subsequent TorrServer `action=list` response. A release with zero seeds can still be accepted and listed, so it will correctly be treated as an importer success and will not reach blocklisting. The required live test must use a release whose hash remains absent through all three add/verification attempts. Then confirm the terminal row, `RADARR_BLOCKLIST_BLOCKED` (or a verified `RADARR_BLOCKLIST_ALREADY_PRESENT` result after an ambiguous response), the actual Radarr blocklist entry and the final grouped Apprise delivery. This end-to-end terminal path has not yet been observed on MINI-Ubuntu.

## Seerr Watchlist tracking (v1.3.0)

When seerr_watchlist_enabled = true, terminal TorrServer failures in pending_adds and permanent preparation errors in terminal_events are registered in the administrator's Seerr Watchlist. The feature adds an attention marker only; it does not create a Seerr media request or trigger a Radarr/Sonarr search.

Useful local journal events:
- SEERR_WATCHLIST_ADDED: added to the Seerr Watchlist.
- SEERR_WATCHLIST_ALREADY_PRESENT: Seerr returned 409 and the exact administrator Watchlist entry was verified.
- SEERR_WATCHLIST_FAILED: registration failed or the TMDb ID is missing.
- SEERR_WATCHLIST_SUMMARY: per-run counts.

The Watchlist state is stored in the existing SQLite database. Do not delete the database to reset notification behavior. Retryable Seerr errors can be retried on later timer runs.

**Telegram behavior:** there is no separate Watchlist warning. The result is added to the single final critical transfer-failure message: the text says the item was added/already present only when that status is confirmed, and reports a registration problem otherwise. The user is also given the Arr card link and advised to choose another release. If Seerr is temporarily unavailable, the message reports that registration could not be confirmed and that arr2torr will retry it; no duplicate follow-up message is sent.

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

An ADD_ACCEPTED log entry means TorrServer acknowledged the add request; it is not a completed import. ADDED is logged after a later TorrServer action=list response contains the exact hash. List membership confirms presence only; it does not guarantee streamability.

The importer submits eligible candidates first, then polls one shared TorrServer action=list snapshot at a configurable interval. Each attempt has its own deadline measured from the add request. Defaults are a 120-second verification deadline and a 3-second polling interval. Compare the deadline with TorrentDisconnectTimeout in the installed TorrServer build.

The shared state database contains:
- imports: confirmed import history and the existing intentional-removal safeguard;
- pending_adds: in-flight, retry-waiting, or exhausted attempts. This table stores safe descriptive fields but not the original download URL or any API key.

After restart, pending attempts are checked against TorrServer before another request is sent. A present but not-yet-ready hash stays pending until the verification deadline. Confirmed attempts are atomically promoted into imports. Expired attempts use exponential backoff, capped at one hour; the default maximum is three add attempts. After that limit the attempt is left in failed state and normal polling does not automatically resend it.

To explicitly re-queue exhausted unverified attempts for one source instance, run:

    sudo -u torrimport /opt/torr-arr-importer/importer.py --config /etc/torr-arr-importer/radarr-1080.toml --retry-failed

Do not combine --retry-failed with --dry-run. This option re-queues only failed unverified attempts for the selected instance; it does not clear successful import history or automatically restore confirmed user-removed torrents. Use --rebuild only for intentional historical repopulation.

Verification means the exact hash appears in TorrServer's action=list response. It does not guarantee that media metadata is ready, that playback will work, or that TorrServer has acknowledged a durable disk commit.

## API and authentication errors

- Arr API calls use X-Api-Key; check that each config contains the key for the correct Arr instance.
- Arr URLs must be base URLs; the importer appends /api/v3/history and /api/v3/movie/{id} or /api/v3/series/{id}.
- The TorrServer URL must point to the receiving server and its /torrents API must be reachable from the service account's host.
- A Prowlarr download URL may contain an API key in its query string. The importer redacts common API-key query parameters in logs and does not save the original downloadUrl into TorrServer metadata.
- HTTP 401/403 and other permanent 4xx failures are not retried repeatedly. HTTP 408, 429, 5xx and transient network failures are retryable.

## Apprise notifications (terminal-only)

The importer sends one user-facing notification type: a grouped final critical message (type=failure). There is **no notification for pending or retry_wait** and no separate Seerr Watchlist alert. Automatic retries remain visible in the local journal but are silent in Telegram until the failure is terminal or a permanent preparation error prevents the add.

Each final item includes the media title, selected release title where available, a human-readable reason, attempts used or a permanent-error explanation, the recommendation to choose another release, an optional direct Radarr/Sonarr card link, and the Seerr Watchlist result if that integration is enabled. Large batches are split only as needed to respect message limits; records without TMDb IDs are not merged together.

Delivery state is stored additively in the existing state.db (notification_attempt_count, notification_next_retry_at, notification_last_error, and notification_attempted_at on terminal rows). A row is marked delivered only after Apprise returns HTTP 2xx. Failed sends are retried using persisted delays of 1, 5, 15, 30 and then 60 minutes (one-hour cap), and survive process restarts. Delivery failure does not alter import state or fail the importer solely by itself. This is at-least-once delivery: a timed-out request may have been accepted by Apprise before the response was lost, so a retry can occasionally duplicate a message.

Check local diagnostics:

    journalctl -u torr-arr-importer@radarr-1080.service -n 150 --no-pager
    grep -E 'APPRISE_(SENT|FAILED|SKIPPED|TERMINAL_)' /var/log/torr-arr-importer/radarr-1080.log

A valid configuration is opt-in and contains notifications_enabled = true, apprise_url, apprise_config_key = "arr-stack", a stable service_name, a short timeout and notification_tag_critical = "critical". The old notification_tag_warning field is still accepted for compatibility but is no longer used to send user notifications. Telegram credentials and topic IDs belong only in Apprise.

## Timers and overlapping runs

Every instance has an independent systemd timer and lock file. The default interval is five minutes. If the four instances are enabled together, their default timer settings may align; use per-instance timer overrides or enable them at staggered times to distribute API load.

If a service is running longer than expected:

    systemctl status torr-arr-importer@radarr-1080.service --no-pager
    journalctl -u torr-arr-importer@radarr-1080.service -n 200 --no-pager

A lock conflict indicates a run of the same configured instance is already active. Do not point different instances at the same explicit lock file; their default lock names differ by instance.

## What the importer will not fix

This service transfers the selected release and the safe metadata subset into TorrServer. It does not resolve a bad indexer result, configure Arr Quality Profiles or Custom Formats, manufacture missing IDs, generate NFO, or materialize the STRM tree. Those belong to the indexer/Arr configuration and downstream library-generation stages.

For the current field mapping and known gaps, see [Architecture](ARCHITECTURE.md) and [Roadmap](../ROADMAP.md).
