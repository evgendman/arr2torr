# Roadmap

This document records the current **torr-arr-importer v1.3.1** implementation separately from remaining work and live validation.

## Unreleased notification UX and delivery revision

The previously documented v1.2 notification behavior (numeric retry warnings plus separate terminal and Seerr messages) is historical and is superseded by this revision.

- [x] Suppress user-facing messages while automatic add/verification retries remain possible; do not send anonymous count-only warnings.
- [x] Send only the final grouped critical notification at terminal failure or permanent preparation error.
- [x] Use natural Russian wording: explain that the selected release could not be transferred to TorrServer, state the useful reason, recommend selecting another release in Radarr/Sonarr, and include the direct card link where available.
- [x] Include the current Seerr Watchlist result in that same message; do not send a second Watchlist warning or separate retry warning.
- [x] Keep different terminal errors separate when their TMDb IDs are missing.
- [x] Mark a terminal notification delivered only after Apprise responds with HTTP 2xx. Store retry count/time/error in the existing SQLite DB, retry delivery with a persistent backoff capped at one hour, and do not alter transfer state.
- [x] Add regression coverage for quiet retry states, one final message with Seerr status, distinct no-TMDb records, and delivery recovery after an Apprise failure.
- [ ] Review CI on Python 3.11/3.12 and merge the change.
- [ ] Deploy a reviewed release to MINI-Ubuntu and inspect an actual Telegram message before treating the wording as live-accepted.

Delivery is at-least-once: after an ambiguous timeout Apprise may already have accepted a message, and a retry can rarely duplicate it. No exactly-once guarantee is claimed. The application version on MINI-Ubuntu remains v1.3.1 until the new release is deliberately installed.


## Implemented

- One standard-library Python implementation for both Radarr and Sonarr.
- One systemd service/timer instance per configured Arr instance.
- Polling the paginated Arr /api/v3/history endpoint on each run.
- Filtering to usable Grabbed records and normalizing/deduplicating their BitTorrent infohashes.
- Retrieving the magnet or direct torrent URL through the history data.downloadUrl, including resolution of HTTP redirects.
- Snapshotting TorrServer through POST /torrents with {"action":"list"}.
- Adding through TorrServer POST /torrents with title, movie/tv category, safe custom data JSON and save_to_db: true.
- Fetching Radarr movie or Sonarr series details to obtain trustworthy provider IDs when they are missing from history.
- Shared SQLite success history across all Arr instances, plus a pending-add table for restart-safe asynchronous verification.
- Per-instance lock files, retry handling for transient HTTP/network failures and redaction of API-key-like URL parameters.
- --dry-run and --rebuild.
- Protection against automatic re-adding after a user removes a known imported torrent from TorrServer.
- Useful summary logs (HISTORY, SNAPSHOT, ADD, ADD_ACCEPTED, ADDED, VERIFY_FAILED, FAILED, DONE).
- Batch verification of accepted add requests using per-hash deadlines, shared list polling and readiness/database-presence evidence.
- Atomic pending claims across Arr instances, persistent retry state, bounded exponential backoff and explicit --retry-failed recovery.

## Validated so far

- **Radarr live path (2026-10-22):** newly grabbed films were discovered on timer runs and added to TorrServer. Successful imports were confirmed by observable file metadata (`file-stats`); later ordinary runs skipped already-present or previously handled hashes. A larger batch also exercised the verification timeout and bounded retry paths.
- **Sonarr live path (2026-10-22):** new Sonarr Grab records were discovered and processed under category `tv`. Several new series releases were accepted and confirmed by `file-stats`. One item remained absent from TorrServer's list beyond the 120-second verification deadline on repeated attempts, so it stayed in bounded retry state; this is a release-level verification failure, not a failure of the Sonarr integration path as a whole.
- **Retry and terminal-failure handling:** a Radarr batch exhausted the configured three verification attempts for some items and produced both an aggregate `warnings` message and a grouped `critical` message.
- **Live Apprise/Telegram delivery (2026-10-22):** five warning notifications (tag `warnings`) and one critical notification (tag `critical`) were delivered during the observed tests. Each was corroborated by `arr2torr` logging `APPRISE_SENT` with HTTP 200 and Apprise logging `Sent Telegram notification`, `Delivered Notification(s)`, and HTTP 200 for the corresponding `/notify/arr-stack` request.
- **One earlier notification anomaly remains unresolved:** at 10:36:52, `arr2torr` logged `APPRISE_FAILED` with HTTP 424 and Apprise logged a Telegram connection error. The user reported receiving an importer warning after deployment, but its exact receipt time was not established well enough to correlate it conclusively with that request. No comparable failure occurred in the later observed deliveries from 11:55 to 13:07.
- **Installed runtime (2026-10-22):** `/opt/torr-arr-importer/importer.py --version` returned `1.2.0`; the installed file compared byte-for-byte equal to the prepared v1.2.0 source. Both `torr-arr-importer@radarr-1080.timer` and `torr-arr-importer@sonarr-1080.timer` were enabled and active. Their oneshot services completed with `status=0/SUCCESS` on timer runs.
- **Idempotency and safeguards:** previous Radarr checks confirmed that a normal run skips hashes already in TorrServer and does not automatically resurrect an intentionally removed, previously imported hash; rebuild remains explicit.
- **Automation:** GitHub Actions passed both Python 3.11 and 3.12 jobs with 19 unit tests at commit `74ee8ac9a8a171be790b5cc314b888cf2e5e19f5`. Coverage includes compilation, notification aggregation/deduplication, terminal summaries, redaction, non-fatal Apprise errors and in-place migration of a v1.1.0 SQLite database.

## Live deployment validation (2026-10-22)

The v1.2.0 package is installed on MINI-Ubuntu and has been exercised through the live Radarr, Sonarr, TorrServer and Apprise integrations. Both configured 1080p timers are active. New film and series records have been submitted to TorrServer, and successful cases were confirmed by post-add verification rather than relying only on TorrServer's immediate `Torrent added` acknowledgement.

Warnings and critical failure summaries have reached Telegram, with successful delivery logged at both the importer and Apprise layers. One earlier attempt returned HTTP 424 while Apprise logged a Telegram connection error; the exact relationship between that request and a warning the user remembers receiving is not conclusively established. Later warning and critical deliveries were all confirmed successful. Keep this as a monitored, isolated anomaly rather than claiming that the failure is either a proven lost message or a proven false-negative delivery result.

The successful integration tests do not imply that every torrent will become ready. Individual releases can remain absent from TorrServer's list until the 120-second verification deadline and proceed through the configured bounded retries. This is surfaced through warning/critical notifications; it does not change the importer's rule that only verified additions enter successful import history.

## Historical deployment baseline and working version (2026-10-21; superseded by the 2026-10-22 live verification above)

**Version recorded as installed on MINI-Ubuntu:** torr-arr-importer **v1.1.0**, dated 2026-10-20, with the installed checkout recorded as commit **7831553da914b147d65989699b34a84c3ce238e5**. The current source file declares VERSION = "1.1.0"; the current installer also reports v1.1.0. The latest source history after the deployment record contains documentation updates; this does not itself prove that the live file on the host has not been changed manually. Run the on-host checks below before treating the installed file and enabled timers as freshly verified.

~~~bash
sudo /opt/torr-arr-importer/importer.py --version
sudo grep -n '^VERSION =' /opt/torr-arr-importer/importer.py
sudo sha256sum /opt/torr-arr-importer/importer.py
systemctl list-timers --all 'torr-arr-importer@*' --no-pager
for instance in radarr-1080 radarr-4k sonarr-1080 sonarr-4k; do
  echo "===== $instance ====="
  systemctl is-enabled "torr-arr-importer@$instance.timer" 2>&1 || true
  systemctl is-active "torr-arr-importer@$instance.timer" 2>&1 || true
done
~~~

The installed version check is read-only. A systemd oneshot service can be inactive between runs; timer state and next-elapse time are the primary signals for periodic scheduling. A prior investigation stopped some timers and inspected pending retry state, so the current runtime state must be read from the host rather than inferred from GitHub. Do not delete or reset the SQLite state database while checking.

### On-host verification (2026-10-21, 17:35 Asia/Yekaterinburg)

The user ran the read-only checks on MINI-Ubuntu and reported:

- /opt/torr-arr-importer/importer.py --version returned 1.1.0.
- The installed file reports VERSION = "1.1.0".
- git hash-object /opt/torr-arr-importer/importer.py returned 082d54b07613e789ae9a1bb9ac7113c97776d4e7, identical to the Git blob SHA of current main:src/importer.py. The installed program file therefore matches the repository's current source file byte-for-byte.
- torr-arr-importer@radar-1080.timer is enabled and active.
- torr-arr-importer@sonarr-1080.timer is enabled and active.
- torr-arr-importer@radar-4k.timer is disabled and inactive.
- torr-arr-importer@sonarr-4k.timer is disabled and inactive.
- systemctl list-timers showed the next run for the two enabled timers around 17:39 on 2026-10-21.

This confirms the installed importer file and the current timer state at that time. The timer state is recorded as observed, not as a recommendation to enable either 4K instance. No SQLite state or service configuration was changed by this verification.


The v1.1.0 behavior already implemented and documented includes asynchronous verification of TorrServer adds, durable pending_adds state, bounded exponential backoff, a default maximum of three add attempts, recovery using --retry-failed, and keeping unverified hashes out of successful import history. On 2026-10-21 the alternative release of *All Good Things (2010)* was confirmed to travel through the usual Arr → TorrServer → torr2strm → Jellyfin chain successfully. The original release failure was not conclusively attributed to a single cause; the controlled alternative-release test indicated the workflow itself can operate successfully.

## Apprise notifications for retry and final transfer failures (implemented in v1.2.0, 2026-10-21)

**Status: implemented and live-validated on MINI-Ubuntu on 2026-10-22.** The live results are recorded in the validation section above. Follow the shared infrastructure contract in [media-server-tuning: notification contract](https://github.com/evgendman/media-server-tuning/blob/main/docs/notification-contract.md) and the actual tags in [Apprise notification routing](https://github.com/evgendman/media-server-tuning/blob/main/docs/apprise-notifications.md).

### Required behavior

- Keep detailed logs as the diagnostic source of truth. Telegram messages are an additional summary/action channel, not a copy of every log line.
- Route interim retry warnings to tag **warnings** with Apprise type **warning**.
- **Before the retry process reaches a terminal failure, do not include movie/series titles, release names, hashes or other item-level identifiers.** Send aggregate numeric information only: number of currently unverified/problematic items and attempt information based on the add-attempt counter, for example a count grouped by attempt number when items are at different stages.
- Do not generate a separate Telegram message for every torrent. Aggregate interim state at the end of a run and avoid repeating an unchanged warning on every timer invocation. Notify when the pending count or retry state meaningfully changes.
- When an item reaches terminal failure—normally after max_add_attempts is exhausted, or sooner if the failure is classified as permanent/non-retryable and no automatic retry will occur—route a final message to tag **critical** with Apprise type **failure**.
- The final message must identify the affected Arr instance and list the human-readable movie/series title. Include the selected release title when available, attempts used versus the limit (or indicate a permanent non-retryable error), and a sanitized short reason. Do not include the credential-bearing download URL, API keys or unnecessary full hashes.
- Produce one aggregated final notification per run/state transition for the set of newly terminal items, not one message per failed torrent. Respect Telegram message-size limits; if a very large list must be split, split into the fewest practical messages while preserving a clear total count.
- Avoid duplicate final alerts on later timer cycles. Persist or otherwise reliably derive whether a particular terminal transition has already been notified. If notification delivery fails, log the problem locally without causing the importer itself to fail or recursively trying to notify about notification delivery.
- Keep automatic retry policy and selection logic unchanged as part of the initial notification integration. This work adds observability; it does not add infinite retries or silently re-queue exhausted items.

### Direct link to Arr

Try to include a clickable link to the exact Radarr movie or Sonarr series card in the final terminal-failure message so the user can open Arr and choose another release without searching manually.

- Build a link only when a trustworthy Arr media ID and a client-reachable UI base URL are available.
- Verify the actual Radarr and Sonarr UI route patterns against the installed versions before generating links. The API base URL configured for the service is not automatically guaranteed to be reachable from the Telegram client; add a separate optional UI base URL if necessary.
- Never put API keys or credential-bearing download URLs into the link.
- A link is optional: lack of a verified card URL must not prevent sending the title, failure summary and log reference.

### Event and state semantics

The low-level HTTP/network options retry_count and retry_delay control request retries; do not confuse them with the TorrServer add/verification counter stored in pending_adds. Use attempt_count and max_add_attempts for notification language about the add attempt. A retryable verification failure remains a warning while another automatic attempt is still scheduled. A terminal state sends a named final failure only once per terminal transition.

### Implementation and test checklist

- [x] Inspect the existing code and pending_adds schema for media title, release title, Arr instance, attempt count and terminal failure state.
- [x] Add opt-in notification settings following the shared contract; keep all Telegram secrets and topic IDs in Apprise.
- [x] Implement a standard-library Apprise HTTP client with a bounded timeout and non-fatal delivery failures.
- [x] Aggregate retry-wait warnings by source instance and attempt distribution; warning bodies contain no item-level identifiers and unchanged fingerprints are suppressed.
- [x] Aggregate newly terminal failures per source run, including media/release titles, attempt information and sanitized causes.
- [x] Persist retry-warning fingerprints and terminal notification-attempt markers in SQLite; baseline old failed rows during migration.
- [x] Add a separate optional Arr UI base URL and best-effort /movie/<titleSlug> or /series/<titleSlug> links; omit links when unavailable.
- [x] Add unit coverage for count-only warning aggregation/deduplication, grouped terminal failures, permanent preparation failure, UI link formatting, URL/hash redaction, disabled-to-enabled delivery, non-fatal Apprise outage and v1.1.0 SQLite migration.
- [x] Assert interim warnings omit names and hashes while terminal alerts include the media/release context and optional link.
- [x] Document notification configuration, upgrade safeguards and operations; CI passed on Python 3.11/3.12.
- [x] Validate live warning and critical Telegram delivery on MINI-Ubuntu on 2026-10-22; correlate importer and Apprise logs.


## v1.2.0 implementation record (2026-10-21)

The code is versioned as v1.2.0. This change adds notifications only; it does not alter `max_add_attempts`, `retry_backoff_sec`, retry eligibility, or the conditions required to record a verified TorrServer import.

Implemented behavior:
- Numeric `warnings` summaries for retry-wait items, deduplicated with an SQLite fingerprint based on count and attempt distribution.
- A named `critical` group for newly terminal TorrServer failures and permanent HTTP 4xx preparation errors, including media title, release title when available, sanitized reason and an optional Arr card link.
- One-time terminal-notification markers, bounded Apprise HTTP timeout and non-fatal send errors.
- In-place SQLite column migration; pre-existing `failed` rows are baselined to avoid a flood of stale notifications after upgrade.
- Four configuration examples default to notifications disabled. The intended MINI-Ubuntu deployment will enable only `radarr-1080` and `sonarr-1080`, matching the last observed live timer state; 4K timers stay disabled.

The GitHub Actions workflow passed on both Python 3.11 and 3.12 with 19 unit tests for source/test commit `74ee8ac9a8a171be790b5cc314b888cf2e5e19f5`. Live deployment is now separately verified on MINI-Ubuntu; see the 2026-10-22 results above.

## Planned improvements

### 1. Register terminal TorrServer transfer failures in Seerr Watchlist

**Status: implemented in v1.3.0; 29 unit tests pass after the v1.3.1 concurrent-migration fix. Live Seerr API validation and production activation remain pending until a real API key is configured.**

**Goal**

When an Arr release reaches a terminal transfer failure in TorrServer, place its movie or series in the administrator's Seerr Watchlist. This gives the user one UI in which to review media that needs manual attention, without creating a Seerr request or deliberately triggering another Radarr/Sonarr search. The Watchlist entry is an attention marker, not a retry or download request.

**Configuration**

- Add an opt-in setting (disabled by default) to the existing TOML configuration.
- Add the Seerr base URL (scheme, host and port) and Seerr API key.
- Reuse the existing arr2torr SQLite state database, timer/service, logging, HTTP timeout and Apprise configuration. Do not create a separate service, systemd timer, state database or database connection path.
- Use the account associated with the API key. Seerr's API-key authentication defaults to administrator user ID 1 unless an explicit `X-API-User` header is supplied; this feature should omit that override and place all entries in the administrator's personal Watchlist.

**Selection and API behavior**

- Process terminal failures from both `pending_adds` rows with `status = 'failed'` and `terminal_events` records for permanent preparation failures, scoped to the currently configured Arr instance/source. Do not process `pending` or `retry_wait` records.
- Use the `tmdb_id` already persisted by arr2torr and the configured media type (`movie` for Radarr, `tv` for Sonarr). Do not perform a separate TMDb lookup or attempt ID conversion in this feature.
- `terminal_events` currently does not persist `tmdb_id`/`tvdb_id`; extend its schema and `record_terminal_event` call to save provider IDs when Arr media details were fetched. If the ID is unavailable in the failure record, do not call Seerr or resolve it externally: record the missing-ID error and notify.
- If `tmdb_id` is missing or invalid, do not call Seerr. Record a clear error/state and make the item visible in logs and an error notification.
- Add films through `POST /api/v1/watchlist` with `tmdbId` and `mediaType: "movie"`. Add series with `tmdbId` and `mediaType: "tv"`; Seerr Watchlist is title-level, so add the whole series. Do not send season or episode fields.
- A successful `201` response means the item was added. Do not perform a full Watchlist scan before every post.
- Handle duplicate responses idempotently. A `409` must not automatically be treated as success, because Seerr can also return it for a database error. On `409` only, inspect the API-key user's Watchlist and confirm that the same media type and TMDb ID are already present before marking it as already registered.
- Preserve the original TorrServer failure status. Store Watchlist processing in separate fields on the existing state row, for example `seerr_watchlist_status`, `seerr_watchlist_last_error`, and a notification/deduplication marker. Use an in-place SQLite migration; never repurpose `status`, retry counters or the existing Apprise notification markers.
- Successful Watchlist registration must not change Arr monitoring, trigger searches, resend magnets or alter the existing bounded retry policy.

**Notifications and logging**

- Log each meaningful outcome with the Arr instance, media type/title and TMDb ID where available: added, already present, skipped due to missing ID, and failed. Never log the Seerr API key.
- When a terminal TorrServer transfer failure has been successfully represented in the administrator's Seerr Watchlist, send a warning through the existing Apprise integration using type `warning` and tag `warnings`. The message must explain that the media could not be added to TorrServer and has been added to the administrator's Seerr Watchlist for review and troubleshooting. This is a warning about the TorrServer transfer failure, not a notice that the media is available to watch. Aggregate newly handled items per run where practical rather than sending one message per release. If Seerr confirms the item was already in the Watchlist (`409` plus verified membership), the failure can still be considered represented there; deduplicate the warning by terminal-failure record so it is not repeated every timer run.
- If Watchlist registration fails (including a missing TMDb ID), log the error and send an Apprise failure notification to tag `critical`, including the title when known and a short sanitized reason.
- Persist notification/deduplication state so the same terminal failure is not announced on every timer run. Transient Seerr/network errors remain eligible for a later retry. Apprise delivery failure is logged locally and must not crash arr2torr or cause recursive notifications.
- Reuse the existing Apprise URL, config key and `notification_tag_warning` / `notification_tag_critical` settings from the current arr2torr configuration and shared notification contract. Route Watchlist-confirmed terminal transfer failures to `warnings` with type `warning`; route failures to register the item in Seerr (including missing/invalid TMDb ID) to `critical` with type `failure`. Keep Telegram destinations/topic IDs inside Apprise, not in arr2torr.

**Manual API validation before production activation**

- Send a manual `POST /api/v1/watchlist` for one known test film using the configured API key, then verify that it appears in the intended administrator's Watchlist in the Seerr UI.
- Repeat the POST for the same film and record Seerr's exact duplicate response.
- Test `GET /api/v1/user/1/watchlist` (including pagination if needed) to confirm how an existing item can be verified after a `409`. Do not rely on `409` alone as proof of duplicate success.
- Repeat the add-and-view check for one test series, confirming it is added at series level.
- Clean up test entries manually if appropriate; do not run these mutations against a real failed item without explicit selection.

**Implementation and test checklist**

- [ ] Validate manual Seerr API behavior (new item, duplicate item, reading Watchlist, and series).
- [x] Add opt-in Seerr URL/key settings and safe validation/redaction.
- [x] Add in-place SQLite migrations for separate per-failure Watchlist state and notification deduplication on both `pending_adds` and `terminal_events`; add provider-ID fields to `terminal_events` and populate them for new events where Arr details are available.
- [x] Select `failed` pending-add rows and permanent `terminal_events` for the active source; use persisted TMDb IDs only, with no ID conversion or external lookup.
- [x] Implement title-level movie/series Watchlist POSTs, and on `409` verify membership before treating it as an existing item.
- [x] Add grouped warnings to Apprise `warnings` (type `warning`) explaining that the TorrServer transfer failed and the item is now in the administrator's Seerr Watchlist; send `critical` (type `failure`) notifications if Watchlist registration itself fails. Deduplicate by terminal-failure record and keep delivery failures non-fatal.
- [x] Add tests for status/source filtering, missing IDs, payload types, duplicate verification, retry after temporary API failure, notification deduplication, secret redaction and migration of an existing SQLite database.
- [ ] Test `dry-run` behavior and a live one-film/one-series path before enabling it for the configured production instances.
- [x] Document settings, log events, Apprise routes, duplicate behavior and recovery/operations.


### 2. Preserve more useful Arr metadata

v1.0.3 reads the history quality, languages and customFormats fields but the current TorrServer data payload does not persist those complete objects. Consider adding a carefully versioned and sanitized subset, for example:

- quality name/source/resolution from the grabbed decision;
- selected audio languages;
- Custom Format names and scores;
- protocol and release-type metadata where useful;
- other stable fields that help downstream NFO generation or release display.

Only non-secret, stable metadata should be stored. Credential-bearing downloadUrl and similar links must remain excluded.

### 3. Expand repeatable end-to-end Sonarr validation coverage

A live new Sonarr Grab was confirmed on 2026-10-22: the timer discovered new series entries, sent them to TorrServer under category `tv`, and verified several successful additions using `file-stats`. One release timed out twice because it remained absent from the list; keep automated coverage for this failure and bounded-retry path, as well as successful import and next-run idempotency.

### 4. Make timer cadence easier to configure per instance

The current template defaults all instances to five minutes. Systemd timer drop-ins can already override offsets/intervals, but the project should provide clear examples or a supported configuration scheme so four instances can be staggered without guesswork.

### 5. Document and test clean-destination recovery

--rebuild already implements the necessary behavior without deleting shared SQLite state. Add repeatable tests that cover replacing the TorrServer database while keeping importer history, then rebuilding each Arr source without duplicate adds.

### 6. Add repeatable tests and recorded API fixtures

Add tests for:

- paginated history;
- multiple history events with the same hash;
- missing/invalid data, hash or downloadUrl;
- magnet URLs and redirect chains;
- Prowlarr API-key redaction;
- shared import history across instances;
- normal mode versus rebuild;
- TorrServer add responses with missing/mismatched hash;
- asynchronous add behavior: HTTP success/expected hash followed by delayed metadata readiness, missing FileStats, disappearance from list, or `GotInfo` timeout;
- [x] post-add verification timeout leaves the hash out of successful import history and records a bounded retry state;
- [x] multi-torrent batches where one hash becomes ready immediately and another after a later poll, verifying shared polling rather than serial full-timeout waits;
- [x] restart recovery from a persisted SQLite pending attempt without resending a hash already present and ready;
- [x] concurrent pending-claim guard and retry-limit/backoff transitions;
- transient vs permanent HTTP status handling.

### 7. Verify asynchronous TorrServer adds before recording successful imports

**Status: core implementation in v1.1.0; Python 3.11/3.12 CI passes. Live integration and additional failure-fixture coverage remain.**

**Reason**

TorrServer's `POST /torrents` `action=add` response is not necessarily proof that the asynchronous metadata-acquisition step completed or that `save_to_db=true` reached its database-save path. The response can contain the expected hash before the server's background `GotInfo()` operation finishes. If metadata acquisition later times out, TorrServer may close the in-memory torrent without saving it, while the current importer has already written the hash to its shared SQLite import history. This can leave a false success marker that suppresses normal retries.

**Work**

- [x] Treat the add response as an acknowledgement that the request was accepted, not as final proof of successful persistence.
- After `action=add`, independently poll TorrServer using its supported list/get APIs for the exact normalized infohash. Require observable readiness evidence (prefer a usable, non-empty file list/FileStats or a trustworthy ready/working status), not merely an immediate echo of the requested hash.
- [x] Send eligible candidates first and track each accepted add as its own pending operation, with its own deadline starting at request submission.
- [x] After submission, use a bounded shared `action=list` polling loop to check pending hashes together; fast-ready torrents are verified while slower torrents continue waiting.
- [x] Keep polling until every pending add reaches a terminal result or its individual deadline expires; individual deadlines prevent an unbounded wait.
- [x] Make verification timeout and polling interval configurable, and document that defaults must be compared with the installed TorrServer timeout.
- [x] Log request acceptance, uncertain responses, verification success/failure and polling errors with the hash and observed status.
- [x] Promote a hash into successful-import history only after verification; store pending attempt metadata and status in the shared SQLite database.
- [x] Reconcile persisted attempts against TorrServer before new adds; observe present-but-unready hashes, and retry absent unverified hashes only after bounded backoff and within the attempt limit.
- [x] Keep pending/uncertain attempts separate from successful history and preserve the intentional-removal safeguard.
- [x] Leave unverified attempts out of successful history, preserve failure details, and require a fresh membership check before retry.
- [x] Preserve the deliberate-removal safeguard for verified imports; historical recovery remains explicit via --rebuild.
- [x] Describe the guarantee as observable readiness/database presence, not an independently proven durable disk commit.
- Add recorded API fixtures and tests for immediate success, delayed readiness, a correct hash returned before later timeout/disappearance, malformed/missing/mismatched hashes, transient API errors, retry after unverified failure, concurrent instances, and intentional user removal. Ensure an unverified attempt cannot suppress a subsequent eligible retry.

**Exit criteria**

- A successful add is not recorded as completed solely because the initial response contains the expected hash.
- Delayed or failed asynchronous adds are visible in logs/state and remain safely recoverable without duplicate-add loops.
- A batch does not exit while it still has pending verification before the configured per-hash deadlines; fast successes are confirmed promptly, and one slow torrent does not force separate serial waits for every other torrent.
- Pending additions survive importer restarts and are reconciled against TorrServer before any retry.
- Ordinary retries remain idempotent; confirmed intentional removals are not resurrected.
- Tests demonstrate both the successful readiness path and the “initial API acknowledgement, later metadata timeout” failure path.

### 8. Coordinate with the downstream STRM/NFO project

The importer already preserves provider IDs and selected Arr fields in TorrServer data, which supports a downstream library tree. The related torr2strm repository is currently private. When its public status and API are settled, document an end-to-end example showing Seerr request → Arr Grab → TorrServer → STRM/NFO library.

### 9. Live integration investigation: TorrServer metadata-acquisition timeouts (2026-10-20)

**Status: narrowed by a successful alternative-release control test on 2026-10-21. The standard Arr → TorrServer → torr2strm → Jellyfin path works with the replacement release. The exact cause of the original release's failure remains unproven; no further broad investigation is planned unless the symptom recurs with a better-validated release.**

**Environment and preserved state**

- Installed `torr-arr-importer 1.1.0` from commit `7831553da914b147d65989699b34a84c3ce238e5` on 2026-10-20.
- Tested Radarr instance `radarr-1080` against TorrServer at `http://127.0.0.1:8092`.
- TorrServer process reported MatriX.145 / torrent v1.2.31 and had been running since 2026-09-28 without a systemd restart (`NRestarts=0`).
- The rebuild dry-run found 86 unique Grabbed hashes in Radarr history, 131 hashes in TorrServer, and 7 rebuild candidates. Rebuild was deliberately used; the shared importer DB was not cleared.
- At the time of this 2026-10-20 investigation, diagnostic work stopped the `radarr-1080` and `sonarr-1080` timers. This historical state was resolved on 2026-10-22: v1.2.0 was installed, both 1080p timers were verified active, and subsequent timer runs completed successfully.

**Observed live results**

- In the 2026-10-20 batch run, 6 add requests received the early TorrServer response `Torrent added`. Only `The Fall Guy (2024)`, hash `[hash omitted]`, was confirmed by usable `file-stats` in about 2 seconds; a later `action=list` showed `Torrent in db`.
- Five accepted hashes did not appear in the list during the 120-second verification window: `All Good Things` (`[hash omitted]`), `Barbie` (`[hash omitted]`), `The Ides of March` (`[hash omitted]`), `Inside Out 2` (`[hash omitted]`), and `Anyone But You` (`[hash omitted]`). The TorrServer journal recorded five generic `timeout connection get torrent info` messages around 14:02:53-14:02:54, roughly 90 seconds after the add requests. Those generic lines do not identify their infohashes, so exact per-line attribution is not proven.
- A single-hash retry of `Inside Out 2` (second attempt; no other add request in that run) was accepted at 14:14:51, followed by the TorrServer metadata timeout at 14:16:21. It remained absent from `action=list` after the importer’s 120-second deadline. Batch concurrency is therefore **not required** to reproduce the failure.
- A later single-hash retry of `All Good Things` (second attempt) was accepted at 21:38:55 and produced another `timeout connection get torrent info` at 21:40:25; the importer again found the hash absent at the end of verification. The failure persisted after the stale ffprobe connections below were terminated.
- `The Prestige` (hash `[hash omitted]`) fails earlier with `HTTP 410 resolving download URL` at importer stage `prepare`. No TorrServer add request is sent for this hash. Treat it as a separate expired/unavailable Arr-history download URL issue; there is no evidence that this release or a Blu-ray-disc structure caused the TorrServer metadata timeouts.
- TorrServer settings were observed as `DisableDHT=false`, `DisableTCP=false`, `DisableUTP=false`, `EnableIPv6=false`, `RetrackersMode=1`, `TorrentDisconnectTimeout=30`, `ConnectionsLimit=25`, `PeersListenPort=0` (automatic port selection). The process had TCP and UDP listeners on the dynamically selected port 37671. These observations do not by themselves prove that DHT/tracker/peer discovery succeeds end to end.
- Two stale `ffprobe` processes (PIDs 1297713 and 1489423) had established localhost connections to port 8092. TorrServer-side sockets had about 4.14 MB each queued for send and showed `rwnd_limited:100%`; each peer was identified as `ffprobe`. Both processes were terminated with SIGTERM, and the established sockets disappeared. The later single-hash `All Good Things` retry still timed out, so cleaning these stuck clients was **not sufficient to fix** metadata acquisition. Their causal relationship to the torrent timeouts remains unproven.
- Earlier generic TorrServer metadata-timeout messages also exist in logs from 2026-09-28 (previous process), 2026-10-04, 10-06, 10-15, 10-16, 10-18 and 10-19. This is not a problem introduced solely by importer v1.1.0.


**Follow-up control test (2026-10-21)**

- For the same film, *All Good Things*, the user selected a different release through interactive search. It came from a different tracker and showed more seeders/peers than the original problematic release, whose reported swarm was 5 seeders / 0 peers.
- The replacement release was added successfully and completed the normal chain through TorrServer, torr2strm and Jellyfin without workarounds or component changes.
- This is evidence that the end-to-end integration is functional and that the original failure was likely specific to the original release, its swarm, or its tracker/source availability—not a general failure of arr2torr or the media-server chain.
- The test changed the infohash, tracker and swarm together, so it does **not** identify one exact cause. Stale indexer statistics, unreachable peers, tracker/DHT discovery trouble, or another release-specific issue remain plausible, but unproven.
- The planned controlled comparison is complete. Do not perform another broad rebuild or change TorrServer settings solely to investigate this original release.


**Current conclusion**

- v1.1.0 correctly distinguishes request acceptance from a verified import: only a confirmed hash enters successful importer history; unconfirmed hashes remain in `pending_adds` with bounded retry state.
- The successful alternative `All Good Things` release confirms that the standard end-to-end chain can work. The best current working hypothesis is a release-specific issue with the original release, its tracker, or its swarm. The exact cause is not proven because the alternative test changed the hash, tracker and swarm together.
- Treat stale seed/peer counts and unavailable peers as plausible explanations, not established facts. A syntactically valid magnet can still fail to resolve metadata if no reachable peer supplies it.
- No further broad investigation, custom torrent-file downloader, or random TorrServer setting changes are planned for this case. If similar timeouts recur, first compare against a different, well-seeded release; investigate networking/TorrServer runtime more deeply only if a known-good control also fails.
- `The Prestige` remains a separate issue: HTTP 410 occurs during download-URL preparation before an add is sent to TorrServer. Re-grab/refresh it in Radarr if the release is still wanted.

**Remaining actions**

1. **Preserve importer state during future diagnostics.** The earlier 2026-10-20 investigation stopped timers and manipulated test retry times; this was followed by a successful v1.2.0 deployment on 2026-10-22, when both 1080p timers were active. Preserve backups when relevant and do not delete/reset `state.db` merely to diagnose a single torrent.
2. **Close the broad investigation of the original `All Good Things` release.** The alternative-release test provides the needed control evidence; do not run another broad `--rebuild`, restart TorrServer, or randomly toggle DHT/TCP/UTP for this case.
3. **If the symptom recurs, test one release at a time.** Prefer a different release with independently credible swarm availability. If that known-good release also fails at metadata acquisition, reopen a focused investigation of tracker/DHT/network reachability and TorrServer runtime, recording exact timestamps and list/status results. Do not increase timeouts without evidence.
4. **Handle `The Prestige` separately.** Its `HTTP 410` is a stale/unavailable Arr history download URL; re-grab or refresh the release in Radarr if still wanted. A TorrServer retry cannot repair a URL that fails before the add call.
5. Use `--retry-failed` only as a deliberate recovery action after checking current swarm/source availability, not as a routine diagnostic step.

## Explicitly not planned

- A webhook listener: polling the Arr history is the selected design.
- A Blackhole directory watcher: Blackhole permits Arr's Grab workflow, but the importer uses the Arr API and TorrServer API.
- Downloading media inside the importer: TorrServer is the receiving/streaming service.
- Silently resurrecting user-removed torrents during normal polling: rebuild must remain an explicit action.
