# Roadmap

This document separates the installed torr-arr-importer revision, observed live behavior, historical results and remaining validation gates.

## Current installed revision and live validation (2026-10-29)

- **Installed code:** `torr-arr-importer 1.3.3` on MINI-Ubuntu, deployed from PR #4 branch `feature/radarr-blocklist-v1.3.2` at commit `7e8feb7`. The code-only deployment replaced `/opt/torr-arr-importer/importer.py`; configuration files, SQLite state and systemd units were preserved.
- **Regression tests and CI:** the full local suite passed **42 tests**. GitHub Actions run [#89](https://github.com/evgendman/arr2torr/actions/runs/40748346991) passed on Python 3.11 and 3.12. PR #4 remains open and unmerged; this is the installed test candidate, not a claim that a tagged release has been published.
- **Live timer run review (historical criterion):** for the 10-hour journal window ending 2026-10-29 09:45 (+05:00), the Radarr importer completed **115 runs**. It submitted **79** add requests; all 79 were confirmed by exact infohash membership in a later TorrServer `action=list` response. There were **0 failed additions**, no `ERROR`/exception entries and no `RADARR_BLOCKLIST_*` or `APPRISE_*` events. This establishes list membership under the then-current criterion, but not that all those imports had usable file metadata.
- **Zero-seed test outcome:** `Viking Quest (2014) BDRip 720p | P2` was accepted and appeared in TorrServer's list on the first attempt. This confirmed list membership only; it did not prove that `FileStats` was available or exercise terminal failure/blocklisting.
- **Corrective finding after manual retry (2026-10-29, 10:41 +05):** `--retry-failed` re-queued **32** terminal rows. v1.3.3 logged `added=32 failed=0` because each exact hash appeared in `action=list`. Subsequent `torr2strm` warnings reported missing file metadata or HTTP 404 for several titles in that batch, including *Inside Out (2015)*, *Sabrina The Teenage Witch*, *The Adam Project* and *Turbo*. Therefore, list membership alone is an insufficient success criterion. The installed v1.3.3 code still has this weak criterion; this roadmap update documents the required correction but does not implement or deploy code changes.
- **Still open:** select a release that remains absent from TorrServer's list after all three configured add/verification attempts. Verify that the importer reaches terminal failure, Radarr's native failed-history pipeline actually records the release in its blocklist, and the single grouped critical Apprise message reports the outcome and delivery state. Until that controlled case passes, live blocklist behavior is **not validated**.

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
- Batch verification of accepted add requests with per-hash deadlines and shared polling. The installed v1.3.3 implementation records success on `action=list` hash membership only; follow-up live evidence showed that criterion is insufficient. The required `FileStats` criterion is tracked as corrective work in item 7 below.
- Atomic pending claims across Arr instances, persistent retry state, bounded exponential backoff and explicit --retry-failed recovery.
- Terminal-only grouped critical Apprise messages with persistent delivery retry state, independent of TorrServer retry state.
- Optional Seerr Watchlist tracking for terminal transfer failures.
- Radarr-only native blocklist handling after the configured add/verification attempt limit is exhausted.
- Stale pending-row cleanup when the hash is already in successful import history, and re-arming after a strictly newer Grab for the same hash.

## Earlier validation records (historical)

- **Radarr live path (2026-10-22):** newly grabbed films were discovered on timer runs and added to TorrServer. Successful imports were confirmed by observable file metadata (`file-stats`); later ordinary runs skipped already-present or previously handled hashes. A larger batch also exercised the verification timeout and bounded retry paths.
- **Sonarr live path (2026-10-22):** new Sonarr Grab records were discovered and processed under category `tv`. Several new series releases were accepted and confirmed by `file-stats`. One item remained absent from TorrServer's list beyond the 120-second verification deadline on repeated attempts, so it stayed in bounded retry state; this is a release-level verification failure, not a failure of the Sonarr integration path as a whole.
- **Historical retry and terminal-failure handling (v1.2 notification contract, superseded above):** a Radarr batch exhausted the configured three verification attempts for some items and produced both an aggregate `warnings` message and a grouped `critical` message. The v1.3.2/v1.3.3 implementation suppresses retry-wait notifications and sends only the final grouped critical summary.
- **Live Apprise/Telegram delivery (2026-10-22):** five warning notifications (tag `warnings`) and one critical notification (tag `critical`) were delivered during the observed tests. Each was corroborated by `arr2torr` logging `APPRISE_SENT` with HTTP 200 and Apprise logging `Sent Telegram notification`, `Delivered Notification(s)`, and HTTP 200 for the corresponding `/notify/arr-stack` request.
- **One earlier notification anomaly remains unresolved:** at 10:36:52, `arr2torr` logged `APPRISE_FAILED` with HTTP 424 and Apprise logged a Telegram connection error. The user reported receiving an importer warning after deployment, but its exact receipt time was not established well enough to correlate it conclusively with that request. No comparable failure occurred in the later observed deliveries from 11:55 to 13:07.
- **Installed runtime (2026-10-22):** `/opt/torr-arr-importer/importer.py --version` returned `1.2.0`; the installed file compared byte-for-byte equal to the prepared v1.2.0 source. Both `torr-arr-importer@radarr-1080.timer` and `torr-arr-importer@sonarr-1080.timer` were enabled and active. Their oneshot services completed with `status=0/SUCCESS` on timer runs.
- **Idempotency and safeguards:** previous Radarr checks confirmed that a normal run skips hashes already in TorrServer and does not automatically resurrect an intentionally removed, previously imported hash; rebuild remains explicit.
- **Automation:** GitHub Actions passed both Python 3.11 and 3.12 jobs with 19 unit tests at commit `74ee8ac9a8a171be790b5cc314b888cf2e5e19f5`. Coverage includes compilation, notification aggregation/deduplication, terminal summaries, redaction, non-fatal Apprise errors and in-place migration of a v1.1.0 SQLite database.

## Historical live deployment validation on v1.2.0 (2026-10-22)

At the time of this historical test (2026-10-22), v1.2.0 was installed on MINI-Ubuntu and was exercised through the live Radarr, Sonarr, TorrServer and Apprise integrations. Both configured 1080p timers are active. New film and series records have been submitted to TorrServer, and successful cases were confirmed by post-add verification rather than relying only on TorrServer's immediate `Torrent added` acknowledgement.

Warnings and critical failure summaries have reached Telegram, with successful delivery logged at both the importer and Apprise layers. One earlier attempt returned HTTP 424 while Apprise logged a Telegram connection error; the exact relationship between that request and a warning the user remembers receiving is not conclusively established. Later warning and critical deliveries were all confirmed successful. Keep this as a monitored, isolated anomaly rather than claiming that the failure is either a proven lost message or a proven false-negative delivery result.

The successful integration tests do not imply that every torrent will become ready. Individual releases can remain absent from TorrServer's list until the 120-second verification deadline and proceed through the configured bounded retries. This is surfaced through warning/critical notifications; it does not change the importer's rule that only verified additions enter successful import history.

## Historical deployment baseline and working version (2026-10-21; superseded by the 2026-10-22 live verification above)

**Historical observation (2026-10-21):** the then-installed torr-arr-importer version and source declared v1.1.0, with checkout commit **7831553da914b147d65989699b34a84c3ce238e5** recorded at that time. This section and the checks below are retained as a historical record only; the current installed v1.3.3 revision and timer state are documented at the top of this roadmap.

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

## Historical Apprise notification behavior in v1.2.0 (2026-10-21)

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

The historical v1.2.0 GitHub Actions workflow passed on Python 3.11 and 3.12 with 19 unit tests for commit `74ee8ac9a8a171be790b5cc314b888cf2e5e19f5`. Current v1.3.3 test and deployment results are recorded at the top of this document.

## Planned improvements

### 1. Seerr Watchlist integration for terminal TorrServer transfer failures

**Status: implemented in v1.3.0 and retained in v1.3.3.** The feature uses the administrator's Watchlist as an attention marker; it does not create a Seerr request or cause a Radarr/Sonarr search by itself.

The importer processes terminal TorrServer failures and permanent pre-add preparation failures for the current Arr source. It uses stored TMDb IDs only, adds a movie or whole series to the administrator's Watchlist, records the result in the existing SQLite database and retries eligible transient errors on later timer runs. It does not perform an external ID lookup when the stored ID is missing.

Current notification behavior is different from the original v1.3.0 design described in earlier history: the Seerr result is included in the single grouped critical Apprise failure message. There is no separate Seerr warning or retry-wait notification.

- [x] Implement opt-in configuration, safe API-key handling, in-place state migration, movie/series registration and duplicate-response verification.
- [x] Include the latest Seerr result in the grouped terminal-failure Apprise message.
- [ ] On a real terminal failure, confirm a new Watchlist registration (and its logged state) in Seerr. The recent live journal window had no terminal failures, so it did not exercise a new registration.

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

### 7. Confirm successful additions only after usable TorrServer FileStats are available

**Status: corrective work required.** Installed v1.3.3 uses exact hash membership in `action=list` as success. The 2026-10-29 manual retry showed that this can mark a torrent successful even though later `torr2strm` cannot obtain its file structure or even find the hash by `action=get`. Do not treat the current list-only criterion as the desired final behavior.

**Required success contract**

The immediate `POST /torrents` response is only an acknowledgement that the request was accepted. Seeing the exact hash in `action=list` is also only an intermediate presence signal, not final success. Additions must remain pending until TorrServer exposes a usable, non-empty `FileStats` list for the exact hash. Entries must have positive unique file IDs and unique, safe relative paths sufficient for downstream tree generation. Do not infer success from `Torrent added`, `Torrent getting info`, `Torrent working`, or `Torrent in db` alone; only usable `FileStats` satisfy the downstream contract.

When a hash is listed without usable `FileStats`, keep checking its details within the configured deadline. For a DB-only `Torrent in db` record, explicitly trigger/continue metadata activation through the supported TorrServer path and then poll for usable `FileStats`; do not count the DB record alone as success. Do not blindly issue duplicate add requests while a live object still exists. If the deadline expires without usable `FileStats`, do not promote the hash into successful-import history; persist the unresolved attempt and use the bounded retry policy. A retry must distinguish an absent hash (eligible for another add) from a present but metadata-incomplete hash (requires metadata recovery/re-observation). Keep the existing intentional-removal safeguard and terminal failure/blocklist/notification behavior.

- [x] Treat the immediate add response as an acknowledgement, not final success.
- [x] Submit eligible candidates first and use shared polling with individual deadlines.
- [ ] Make exact-hash presence in `action=list` an intermediate signal only; require usable `FileStats` before promoting an item to successful-import history.
- [ ] Handle `Torrent added` / `Torrent getting info` as incomplete states; do not accept `Torrent working` or `Torrent in db` alone without usable `FileStats`.
- [ ] Implement and test the metadata-recovery path for DB-only/present-but-incomplete hashes without blind duplicate adds.
- [ ] Preserve bounded retries and terminal-failure handling when `FileStats` never arrives; never write such a hash into successful-import history.
- [ ] Update regression tests for hash-visible-without-FileStats, delayed FileStats, metadata timeout, disappearance/HTTP 404, DB-only recovery and the absent-versus-present retry distinction.
- [ ] Live-validate that a newly added release enters successful-import history only after usable `FileStats` is exposed and is then processed by `torr2strm`.
- [ ] Reassess the 2026-10-29 batch of 32 re-queued items against usable `FileStats`; the earlier 32/32 list-presence result is not sufficient evidence of successful downstream readiness.
- [ ] Separately live-validate a release that remains without usable `FileStats` through all three configured attempts and confirm terminal failure triggers the Radarr blocklist and grouped critical Apprise outcome.

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

### 10. Automatically blocklist terminally failed Radarr releases

**Status: implemented in the installed v1.3.3 candidate; regression tests and CI pass. Ordinary live imports pass, but the real terminal-failure-to-Radarr-blocklist path remains unverified.**

**Goal**

Reduce manual intervention by automatically excluding a release from future Radarr selection after arr2torr has exhausted its TorrServer add/verification attempts, allowing Radarr's own failed-download handling to search for a workable alternative according to its settings.

**Configuration and scope**

- Add radarr_blocklist_enabled, defaulting to true in the code and in Radarr example configs. Set it to false to opt out.
- Apply this feature to Radarr only. Sonarr behavior remains unchanged.
- Use the supported native endpoint POST /api/v3/history/failed/{historyId} for the exact Grab history record. The current Radarr API does not expose a direct blocklist-creation POST endpoint; do not invent POST /api/v3/blocklist.
- This native failed-history action deliberately invokes Radarr's ordinary failure pipeline; any replacement search follows the user's Radarr settings.

**Trigger and safety rules**

- Process only pending_adds rows with terminal status = 'failed' and attempt_count >= max_add_attempts after TorrServer add/verification. Do not act on retry_wait, before the configured count is exhausted, or on permanent preparation errors that happened before the TorrServer add.
- Preserve the TorrServer attempt/backoff policy. The blocklist action must not mark an unverified torrent as a successful TorrServer import.
- Track the blocklist state in the existing SQLite database. Add schema columns in place; never reset the shared state database.
- On first migration, mark failures already present before the feature as legacy_skipped, avoiding an unexpected mass of retrospective Radarr actions on upgrade. New terminal failures remain eligible automatically. Explicit --retry-failed resets the blocklist state for a deliberately re-armed attempt.
- Persist request_started before the external side effect. If the HTTP result is ambiguous, verify read-only on later runs and do not blindly repeat the failed-history POST, which may create duplicate blocklist entries or trigger duplicate failed-download handling.
- After an ambiguous request, check GET /api/v3/blocklist/movie?movieId=.... Radarr's public blocklist resource does not expose the torrent infohash, so this recovery path can only match the source release title and must report the limitation; it is not an exact-hash proof. After three successful checks without evidence, record a terminal blocklist failure.

**Apprise notifications and logging**

- Append the blocklist outcome to the existing grouped terminal-failure Apprise message; do not send a separate blocklist-only message.
- Report confirmed native-action success, a source-title match found during recovery, unresolved/ambiguous state, or a definitive failure with a short sanitized reason. Do not mention blocklisting when the setting is disabled, for Sonarr, or for preparation failures.
- Log meaningful outcomes with the Radarr instance, movie/release context and relevant history/media IDs. Use sanitized causes and never log API credentials.
- Preserve existing terminal notification severity, aggregation and deduplication. Apprise delivery errors remain non-fatal.

**Implementation checklist**

- [x] Inspect Radarr's supported blocklist and history/failed APIs; use the native failed-history route rather than an unsupported endpoint.
- [x] Add a default-enabled per-instance setting and explicit true values in Radarr example configs; preserve an opt-out.
- [x] Restrict execution to terminal TorrServer add/verification failures in Radarr; keep retries, Sonarr, and pre-add preparation errors out.
- [x] Add in-place state migration and per-item action/verification status; mark pre-upgrade failed rows as legacy_skipped.
- [x] Add blocklist outcome to the existing terminal Apprise message and structured result logs, including a skipped log when disabled.
- [x] Add tests for default-enabled TOML, exact attempt-limit boundary, native endpoint behavior, Radarr-only scope, ambiguous-result recovery, permanent API errors, state migration, and notification wording.
- [x] v1.3.3 local regression suite: 42 tests passed.
- [x] GitHub Actions run [#89](https://github.com/evgendman/arr2torr/actions/runs/40748346991): Python 3.11 and 3.12 both passed.
- [x] Install the code-only v1.3.3 update on MINI-Ubuntu and validate the normal timer path: 115 completed runs in the reviewed 10-hour window; 79/79 add requests confirmed by list membership; no failed additions.
- [ ] Use a release that remains absent from TorrServer's list after all three attempts; verify terminal state, Radarr's native blocklist entry, and the grouped critical Apprise message with a confirmed delivery result. The zero-seed Viking Quest 720p test did not reach this path because TorrServer accepted and listed it.

## Explicitly not planned

- A webhook listener: polling the Arr history is the selected design.
- A Blackhole directory watcher: Blackhole permits Arr's Grab workflow, but the importer uses the Arr API and TorrServer API.
- Downloading media inside the importer: TorrServer is the receiving/streaming service.
- Silently resurrecting user-removed torrents during normal polling: rebuild must remain an explicit action.
