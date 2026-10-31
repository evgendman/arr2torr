# Operations and troubleshooting

## Understand one v1.4.1 candidate cycle

A normal cycle performs:

1. START — selected instance, Arr type, category and target lane.
2. HISTORY — fetch and deduplicate the configured Arr history.
3. Read the shared SQLite import and pending state.
4. For each unresolved pending hash owned by this instance, query only that exact hash with TorrServer `action=get`.
5. For each new eligible Arr hash, query only that exact hash. If it is absent, prepare metadata, atomically claim it and submit one `action=add`.
6. Inspect every immediate add response. A usable FileStats structure confirms success; otherwise the candidate joins the shared in-run verification queue.
7. After the initial pass across pending and new candidates, poll all unresolved hashes in one carousel using exact-hash `action=get` at the configured `verification_poll_interval_sec`.
8. Remove a hash from the queue immediately when valid FileStats appear. Each hash has its own `verification_timeout_sec` deadline (default 120 seconds); a timed-out hash remains pending for the next timer run.
9. DONE — submitted/added/failed counts, pending count, and elapsed time.

There is no full TorrServer inventory request. Verification is not a separate 120-second serial wait per candidate: one shared queue sweeps all unresolved hashes. An idle run with no new candidates and no pending hashes requiring observation makes **zero TorrServer API requests**.

Useful log events:

- `SKIPPED_PRESENT`: an exact-hash get returned usable FileStats; the hash was recorded in shared verified history.
- `PENDING_EXISTING`: get found an object without usable FileStats; it entered the current run's queue.
- `ADD_ACCEPTED_PENDING`: TorrServer acknowledged add but immediate FileStats were not ready; the hash entered the queue.
- `VERIFY_QUEUED`: a candidate entered the shared carousel with its own timeout.
- `PENDING_METADATA`: an action=get returned the object but its FileStats remain incomplete.
- `PENDING_WAIT_TIMEOUT`: this hash's individual wait expired; it remains pending for the next timer run.
- `PENDING_GET_MISSING`: exact-hash get returned 404 after a prior add. At most one such observation is counted in one importer execution; two separate runs are required before retry/backoff.
- `ADDED ... recovery=action-get-in-run`: a later carousel read found usable FileStats and promoted the pending row.
- `ADD_UNCERTAIN`: add result was ambiguous; the same run's carousel attempts read-only reconciliation.

Example commands:

    journalctl -u torr-arr-importer@radarr-1080.service -n 150 --no-pager
    sudo -u torrimport /opt/torr-arr-importer/importer.py \
      --config /etc/torr-arr-importer/radarr-1080.toml --dry-run

If the timer is enabled, use systemctl list-timers 'torr-arr-importer@*' to check the next schedule.

## State model and deliberate-removal protection

The candidate does not compare complete Arr and TorrServer inventories. It uses:

- **A** — unique Grabbed hashes in the configured Arr history;
- **I** — hashes in the shared SQLite verified-import history;
- **P** — hashes in the shared SQLite pending table.

Normal mode excludes **I** without calling TorrServer. It observes only relevant rows in **P** and new hashes in **A − I − P**. If there is no relevant pending work and no new candidate, no TorrServer request is sent.

Two distinct safeguards follow:

- **Cross-instance duplicate protection:** every instance shares the same pending claim and verified-import state. Only one instance can own an unresolved hash and send its add request.
- **Deliberate-removal protection:** once usable FileStats have confirmed a hash and it is in I, normal runs do not query TorrServer for it. Removing it from TorrServer therefore does not cause an automatic resurrection.

A newly encountered hash that already exists in TorrServer with usable FileStats is recorded in I as an observed, ready item. Its FileStats are validated before that record is made.

## In-run verification and retry behavior

TorrServer may return an add acknowledgement before metadata acquisition completes. The importer checks the immediate response, then queues any unresolved hash. Once the initial pass is complete, one shared carousel polls each queued hash through exact-hash `action=get` at the configured interval.

Every hash receives an independent `verification_timeout_sec` deadline (default 120 seconds) beginning when it enters the queue. A successful hash is removed immediately. When a deadline expires, only that hash's current wait ends: the row stays pending for the next timer run. The timeout does not increment the add-attempt count, trigger blocklisting or create a separate metadata-failure counter.

A present but incomplete object is never blindly re-added. In particular, `Torrent in db` is not success. On MatriX.145, the get handler can initiate the DB-backed reload into memory; the same run's later carousel reads observe the result.

If a previously accepted add later returns HTTP 404:

1. The first 404 is recorded but does not trigger an immediate add.
2. A second 404 on a separate importer run moves the unresolved attempt into the existing bounded retry/backoff policy (or terminal failure when attempts are exhausted).
3. A due retry performs another exact-hash get. Only if it is still absent and a current Arr Grab/download URL is available can another add be submitted.

Repeated 404 responses during one in-run carousel count at most once. This reduces accidental re-adds when a user removes a torrent before its first verification run. It cannot perfectly distinguish deliberate removal from a failed metadata initialization; that ambiguity is inherent in polling without an explicit user-removal signal.

Only actual add submissions consume `max_add_attempts`. Hash reservations, get calls, present-but-incomplete objects, network errors, 5xx responses and an individual verification timeout do not. Automatic HTTP retries are disabled for the side-effecting add call; read-only requests retain their configured retry behavior.

## When to use --rebuild

\`--rebuild\` is an explicit recovery/repopulation mode. It ignores the successful-import exclusion **I** for that execution, but still queries only the exact Arr hashes being reconsidered. A ready object with usable FileStats is skipped; an absent object may be re-added after an exact-hash 404 and a successful atomic claim.

Use rebuild when switching to a clean/new TorrServer database, or when you intentionally want to restore source-history releases that were previously confirmed but are now missing. No full TorrServer list is fetched.

Example:

    sudo systemctl stop torr-arr-importer@radarr-1080.timer
    sudo -u torrimport /opt/torr-arr-importer/importer.py \
      --config /etc/torr-arr-importer/radarr-1080.toml --rebuild
    sudo systemctl start torr-arr-importer@radarr-1080.timer

Run rebuild for each relevant Arr instance. Because the instances share SQLite claims and import history, later instances skip hashes already claimed or confirmed by an earlier instance.

**The database does not need to be deleted for rebuild.** Do not remove the shared state file; clearing it erases intentional-removal memory for every instance.

## Why multiple releases of one movie/series are retained

Arr often focuses on the final chosen download in a conventional workflow. However, users can manually Grab different releases over time. Each unique infohash is treated as its own torrent. The importer deduplicates repeated history rows for the same hash, not distinct hashes for the same title.

That means different versions of one movie or series can all be retained when grabbed separately.


## Radarr/Sonarr blocklisting after terminal TorrServer failure

Radarr's native failed-history path was live-confirmed on MINI-Ubuntu on 2026-10-29. Sonarr's v1.3.5 path also ran live that day: five failed series-release choices logged `SONARR_BLOCKLIST_BLOCKED`. The next importer cycle verified replacement additions for Outer Banks S01, Outer Banks S03 and Dark S03. The native blocklist feature is considered live-validated for both apps; replacement search and results remain dependent on each application's settings and the releases available to it.

Every Radarr example TOML explicitly sets `radarr_blocklist_enabled = true`; every Sonarr example TOML explicitly sets `sonarr_blocklist_enabled = true`. Use the matching explicit setting in each active profile. Missing keys default to `true` for backward compatibility, not as a replacement for visible operational configuration. Set the corresponding flag to `false` only when intentionally disabling that application's blocklist behavior.

The importer acts only on `pending_adds` rows whose TorrServer workflow reached `status = 'failed'` and whose `attempt_count` is at least `max_add_attempts`. A permanent API rejection before that attempt limit is reached is not sufficient. Pre-add preparation failures (for example, an expired download URL) are excluded. The importer calls the corresponding application's native `POST /api/v3/history/failed/{arr_history_id}` endpoint; Arr runs its own failed-download and blocklist logic. Any replacement search follows that application's settings.

Look for these log events:

- `RADARR_BLOCKLIST_BLOCKED` / `SONARR_BLOCKLIST_BLOCKED`: native failed-history action returned successfully.
- `RADARR_BLOCKLIST_ALREADY_PRESENT` / `SONARR_BLOCKLIST_ALREADY_PRESENT`: after an ambiguous request, a blocklist entry with the same source release title was found.
- `*_BLOCKLIST_VERIFY_PENDING`: the request outcome is ambiguous; the importer is performing read-only checks and does not resend the failed-history request.
- `*_BLOCKLIST_FAILED`: the required identity is missing, Arr rejected the operation, or verification remained inconclusive after three successful checks.
- `*_BLOCKLIST_SKIPPED`: the corresponding flag is disabled, or a terminal row has fewer attempts than `max_add_attempts`.

For ambiguous-result recovery, Radarr looks up `/api/v3/blocklist/movie?movieId=...`; Sonarr uses its paged `/api/v3/blocklist?seriesIds=...` response and inspects its `records` array. Both compare the source release title because the public blocklist resources do not expose the torrent infohash; this is not exact-hash confirmation. The failed-history POST is not repeated automatically, to avoid duplicate side effects.

Each application's status/error/verification counters are kept in separate SQLite columns and migrated in place. On first migration, pre-existing failed rows are marked `legacy_skipped`; do not reset the database. Explicitly re-arming an exhausted TorrServer attempt with `--retry-failed` resets that instance's state.

The blocklist outcome is appended to the existing grouped terminal Apprise failure notification, not sent as a separate message. If Apprise is disabled, the result remains in the service logs. Both Radarr and Sonarr native blocklist paths are live-confirmed; GitHub Actions run #139 covers the v1.3.5 code and ambiguous-result recovery tests.

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

Use the HISTORY, HASH_SYNC and per-hash events. There is no TorrServer SNAPSHOT stage in v1.4.0.

| Log/result | Meaning and next check |
|---|---|
| skipped_no_data / skipped_no_hash / skipped_no_url | The Arr history record does not contain the data needed to identify or transfer the chosen torrent. |
| SKIPPED_PRESENT | Exact-hash get returned usable FileStats; the hash was added to shared verified history. |
| PENDING_EXISTING / PENDING_METADATA | Exact-hash get found an object but its file structure is not ready. The importer waits for a later timer run and does not send a duplicate add. |
| ADD_ACCEPTED_PENDING | TorrServer acknowledged add, but the immediate response did not contain usable FileStats. It remains pending. |
| PENDING_GET_MISSING | Exact-hash get returned 404. The log reports consecutive observations; the first 404 after an accepted add does not immediately trigger another add. |
| PENDING_GET_FAILED / TORRSERVER_GET_FAILED | The read failed for a reason other than 404. This is an unknown observation, not evidence that the torrent is absent. |
| ADD_REJECTED / ADD_UNCERTAIN | The add was rejected or its outcome is unknown. The pending row is reconciled through later get requests. |
| candidates=0 | No eligible new hashes were found. If there is also no pending work due for observation, TorrServer receives no request. |

## Asynchronous add verification (v1.4.0 candidate)

The immediate action=add response is checked for usable FileStats. ADDED is logged immediately only when the exact expected hash has a non-empty usable file list, positive unique IDs and unique safe relative paths. An HTTP acknowledgement, echoed hash or status string such as Torrent added, Torrent getting info, Torrent working or Torrent in db is never sufficient on its own.

When the add response is incomplete, arr2torr persists the pending state and ends the current run. It does not execute an in-process polling loop, sleep for 120 seconds or fetch a full action=list snapshot. Later systemd timer runs inspect only the unresolved hash with action=get.

A present-but-incomplete object remains pending. On TorrServer MatriX.145, a DB-only Torrent in db get can start reloading the torrent into memory; the importer simply observes it on a later run rather than issuing another metadata-activation add. A 404 after a previously accepted add is recorded, and a second 404 on a separate timer run is required before the existing bounded retry/backoff policy is entered. Network errors and 5xx responses do not count as absence.

The shared state database contains:
- imports: hashes confirmed by usable FileStats; this also protects intentionally removed torrents from automatic resurrection in normal mode.
- pending_adds: unresolved claims, actual add-attempt count, missing-observation count, retry state and notification/blocklist state. The original download URL and API keys are not stored. The missing_observations column is added through an in-place migration.

Only actual action=add submissions increment attempt_count; action=get observations do not consume max_add_attempts. Pending rows survive process restarts. A pending item is promoted to imports only after usable FileStats are observed. --retry-failed explicitly re-queues exhausted unverified attempts, while --rebuild is the deliberate exception to the normal successful-import safeguard.

The 120-second verification deadline and three-second polling interval are retained as accepted legacy configuration keys for compatibility; the v1.4.0 candidate no longer uses them to wait for metadata inside a run. verification_timeout_sec remains stored as legacy diagnostic deadline metadata, not as an expiry that can fail a pending torrent.

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
