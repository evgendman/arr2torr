# Roadmap

This document separates the installed torr-arr-importer revision, observed live behavior, historical results and remaining validation gates.

## v1.4.1 candidate — shared in-run FileStats verification queue

**Status: v1.4.1 was installed and manually live-validated against production Radarr, Sonarr and SQLite on MINI-Ubuntu (2026-10-31); this is an unreleased candidate on branch `feature/in-run-get-verification-v1.4.1`, and draft PR #6 remains open.** GitHub Actions [run #157](https://github.com/evgendman/arr2torr/actions/runs/41210488097) passed on Python 3.11, 3.12 and 3.14 with 61 tests per version, compilation and version check. The installed file reports `1.4.1` and its Git blob SHA `c9943d92a396882765a92e01cb40d28aa7388777` matches candidate source. Production SQLite was backed up and migrated in place; both manual profile runs completed successfully and the final database integrity check returned `ok`. Representative CPU/request-count comparisons remain incomplete. **Timer activation was subsequently confirmed on 2026-10-31:** both `radarr-1080` and `sonarr-1080` are enabled and `active (waiting)`; the reported next trigger for both was 22:09:09 (+05:00). `systemctl list-timers 'torr-arr-importer@*' --all` listed exactly these two 1080p timers; no 4K timer was enabled or started. No tag or GitHub Release has been published.

- Keeps hash-addressed `action=get` and removes full TorrServer inventory polling from ordinary and rebuild processing.
- Performs the initial exact-hash check and submits eligible adds for all candidates before entering one shared verification carousel.
- Polls every unresolved hash through exact-hash `action=get` at the configured interval. A hash is removed immediately when valid `FileStats` appear.
- Gives each hash its own monotonic `verification_timeout_sec` deadline (default 120 seconds), measured from the moment it enters the in-run queue. One slow candidate does not extend another candidate's deadline.
- On timeout, ends only that candidate's current in-run wait. It remains `pending` and is checked again on the next timer run; timeout does not increment `attempt_count`, trigger blocklisting or create a metadata-failure counter.
- Only actual `action=add` submissions consume `max_add_attempts`. Present-but-incomplete objects and read-only `get` observations do not cause duplicate adds.
- Preserves the two-distinct-run 404 safeguard after an accepted add. Repeated 404 responses inside one carousel are counted at most once in that importer execution.
- Keeps the shared SQLite state, atomic cross-instance claims, deliberate-removal protection, retry/backoff, Radarr/Sonarr blocklisting, Seerr Watchlist and notification paths.

**Validation remaining:** representative request-count and CPU measurements across idle, pending, small-batch and larger-library cases; no A/B run of the v1.3.5 binary was possible because its only local backup is root-private and no public v1.3.5 tag exists. CI is green in [run #157](https://github.com/evgendman/arr2torr/actions/runs/41210488097). Production installation, in-place migration, both manual live profile runs and the final SQLite integrity/count checks are now recorded in [the production validation report](docs/VALIDATION-v1.4.1-MINI-Ubuntu-2026-10-31.md). **Automated scheduling confirmed:** both 1080p timers are enabled and `active (waiting)` (systemd status captured 2026-10-31 22:04:09 +05:00); both reported next trigger 22:09:09 +05:00. The timer listing contained exactly two `torr-arr-importer` timers.


## MINI-Ubuntu production installation and live validation (2026-10-31)

**Result: v1.4.1 installed, both 1080p profiles completed manual production runs successfully, and both 1080p systemd timers were then re-enabled and confirmed active (waiting) on 2026-10-31.**

- Installed `/opt/torr-arr-importer/importer.py` reports v1.4.1 and has Git blob SHA `c9943d92a396882765a92e01cb40d28aa7388777`, matching candidate source. Previous v1.4.0 file SHA: `d7b0bc6a84b45ffee9cfd190b18f0a4a66c617d7`. Installed mode/owner: root:root 0755. A v1.4.0 backup was retained at `/tmp/torr-arr-importer-v1.4.0-backup-20261031-213932`.
- Before migration, SQLite backup was created at `/tmp/torr-arr-importer-v1.4.1-pre-migration-20261031-214522/state.db`; integrity_check=ok; imports=728, pending_adds=22, terminal_events=1. Backup SHA-256: `c701a4a5a953f12b59a25b183a54bf1963538fb0126b398fe9200f3661ab6dda`.
- v1.4.1's first StateDB initialization added only `pending_adds.missing_observations` in place. No tables or rows were reset. Post-migration integrity_check=ok; counts remained 728 / 22 / 1.
- Both dry-runs passed without TorrServer requests. Radarr: records=662, grabbed=655, unique hashes=582, duplicates=73, one candidate (Groundhog Day, TMDb 137 / IMDb tt0107048), duration=1.90 s, exit=0. Sonarr: records=1596, grabbed=1590, unique hashes=169, duplicates=1421, candidates=0, duration=5.68 s, exit=0.
- A read-only re-arm audit found no newer Grab for the existing terminal failures: Radarr 0/7, Sonarr 0/15. All 22 pending rows remained terminal with attempt_count=3.
- **Radarr normal run:** Groundhog Day (1993), hash `933b69e6356324d46f23d5dabadb238317c6d1cc`, first returned `Torrent in db` without FileStats. The shared in-run exact-hash GET queue polled again after about three seconds and received usable FileStats. Summary: submitted=0, added=1, failed=0, pending=22; duration=4.03 s. No duplicate add was sent.
- Post-Radarr SQLite check: imports=729; Groundhog Day hash appears once in `imports` and is absent from `pending_adds`; pending total remains 22.
- **Sonarr normal run:** records=1596, grabbed=1590, unique hashes=169, duplicates=1421. Summary: submitted=0, added=0, failed=0, pending=22; duration=5.31 s. Seerr summary: candidates=16, requests=0, added=0, already_present=0, errors=0.
- **Final production SQLite check after both runs:** integrity_check=ok; imports=729; pending_adds=22 (Radarr failed=7, Sonarr failed=15); Groundhog Day hash appears once in imports.
- Immediately after both manual runs, the importer services and timers were observed inactive and no importer process remained. The operator then ran `systemctl enable --now torr-arr-importer@radarr-1080.timer torr-arr-importer@sonarr-1080.timer`. On 2026-10-31 at 22:04:09 +05:00, both timers were confirmed `enabled` and `active (waiting)`; each reported next trigger 22:09:09 +05:00. `systemctl list-timers 'torr-arr-importer@*' --all` listed exactly two timers. The 4K timers were not touched.
- Production traces: `/tmp/torr-arr-importer-v1.4.1-radarr-live-20261031-215942.log` and `/tmp/torr-arr-importer-v1.4.1-sonarr-live-20261031-220128.log`.
- **Outstanding:** controlled CPU/request-count measurements for idle, pending, small-batch and larger-library cases; and a v1.3.5 executable A/B comparison. This functional validation does not prove downstream torr2strm/Jellyfin ingestion or playback.

## Pre-install MINI-Ubuntu isolated smoke test and preliminary API sample (2026-10-31)

**Result: candidate behavior passed in an isolated, read-only test; production deployment was not performed.**

- The exact v1.4.1 source at commit `8dd95a08cc86d7701dfe2a391f8c2e5142fa37be` was downloaded to `/tmp`. Its Git blob SHA matched `c9943d92a396882765a92e01cb40d28aa7388777`; Python 3.14.4 `py_compile` succeeded and `--version` returned `1.4.1`.
- A temporary Arr mock returned one Radarr Grabbed record for Groundhog Day (1993), infohash `933b69e6356324d46f23d5dabadb238317c6d1cc`. The importer used only a temporary TOML config, temporary lock file and temporary SQLite database.
- A local safety proxy forwarded only exact `POST /torrents {"action":"get","hash":"933b69e6356324d46f23d5dabadb238317c6d1cc"}` requests. It would reject all other paths, methods and payloads, including `action=add` and `action=list`.
- First GET returned HTTP 200 with `Torrent in db` and no `FileStats`; the next GET about three seconds later returned HTTP 200 with `Torrent working` and one `FileStats` entry. The importer logged `ADDED ... verification=usable-file-stats recovery=action-get-in-run`, finished in 3.21 seconds, and promoted the hash into the **temporary** `imports` table. Test DB summary: imports=1, pending=0, terminal_events=0, attempts=0.
- The proxy recorded zero blocked calls; no `add` or `list` request was forwarded to TorrServer. This directly validates the DB-only to ready transition through the candidate's in-run exact-hash GET carousel.
- A second negative-path run used a valid but absent 40-character hash. The real TorrServer returned HTTP 404 to the exact-hash GET; the candidate then attempted an add, which the proxy rejected with HTTP 403 (`blocked by read-only smoke-test proxy`). The blocked payload was captured, no add was forwarded upstream, and the resulting failed/attempt_count=1 row existed only in the temporary test SQLite database. This proves the protective proxy gate intercepted an actual add attempt rather than merely being unused during the positive-path test.
- A separate one-request read-only sample against the same TorrServer measured: exact-hash GET = 1,494 response bytes / 0.197 s; full `action=list` = 531,442 response bytes / 0.219 s. The list response was about 356 times larger in this sample, while single-request latency was similar. This is a preliminary response-size comparison, not a representative repeated-load or CPU benchmark.
- TorrServer RSS increased by about 131 MiB across the combined GET+LIST observation window. Since the GET also activated a DB-only torrent and no isolated before/after RSS sample was taken for each request, this change is **not attributed to either request individually**.
- A 39-character hash in the first draft of the test fixture caused HTTP 500; after correcting the fixture to the exact 40-character hash found in TorrServer's local BoltDB, the smoke test passed. This was a test-data error, not an importer defect.
- At the time of this pre-install smoke test, production guardrails remained unchanged: `/opt/torr-arr-importer/importer.py` reported v1.4.0; Radarr/Sonarr 1080p timers were inactive (enabled); v1.4.1 had not yet been run against production SQLite. The later production installation and manual live runs are recorded in the section above.

**Not yet validated at the time of the pre-install smoke test:** CPU deltas under controlled repeatable load, comparison against the v1.3.5 executable, multi-hash queue load at realistic batch sizes, cross-profile concurrency on the host, and production importer execution. Production manual validation was completed subsequently and is recorded above. No public v1.3.5 Git tag was found, and the host's local backup directory is not readable by the unprivileged account; permissions were not changed.

## v1.4.0 superseded draft — non-blocking verification

The earlier v1.4.0 draft on `feature/nonblocking-hash-get-v1.4.0` passed its original CI but removed the in-run polling window. Live analysis of TorrServer MatriX.145 showed that this could leave DB-only objects in pending state between timer runs without actively observing the `Torrent in db → Torrent getting info → FileStats` transition. The draft is retained for history but is superseded by v1.4.1. It must not be deployed as-is.

## Confirmed installed revision and live validation (2026-10-29)

- **Installed runtime/source identity:** `/opt/torr-arr-importer/importer.py --version` returned `1.3.5` on MINI-Ubuntu. The installed file's `git hash-object` is `712d6f4a276602250a25c52c5f5ccf6b0af92667`, identical to the Git blob SHA of the v1.3.5 source file `src/importer.py`.
- **CI:** GitHub Actions [run #139](https://github.com/evgendman/arr2torr/actions/runs/40844001781) passed on Python 3.11 and 3.12 with 56 unit tests per version, compilation and version check (`1.3.5`).
- **Explicit config follow-up:** the last read-only check of `/etc/torr-arr-importer/radarr-1080.toml` found that it omitted `radarr_blocklist_enabled`; that runtime relied on the backwards-compatible default `true`. Recheck the active file and make the setting explicit if not already done. Example TOMLs already state the flag explicitly.
- **Strict success verification live-tested:** 20 new/replacement additions reached `ADDED verification=usable-file-stats`. The immediate TorrServer add acknowledgement and hash list membership were not treated as final success.
- **Radarr terminal blocklist path confirmed:** seven failed release choices reached the configured three-attempt limit and emitted `RADARR_BLOCKLIST_BLOCKED` after Radarr's native failed-history action. Three grouped critical Apprise notifications were acknowledged with HTTP 200 (batches of two, four and one item). Five replacement releases subsequently passed `FileStats` verification.
- **Sonarr terminal blocklist path confirmed:** five failed series-release choices logged `SONARR_BLOCKLIST_BLOCKED` on 2026-10-29. Replacement releases for Outer Banks S01, Outer Banks S03 and Dark S03 subsequently reached `ADDED verification=usable-file-stats`. The logs confirm successful native blocklist actions; no replacement additions for Love, Death & Robots S02/S03 were observed in the reviewed log window. Replacement search results depend on Sonarr's own settings and available releases; the absence of an observed alternative does not make the blocklist action unsuccessful.
- **Systemd status caveat:** cycles with verification failures log their `DONE` summary and exit code 1, leaving the oneshot service marked `failed` even while its timer remains active and schedules later cycles. This matches the current `return 0 if failed == 0 else 1` code path; do not interpret the unit state alone as proof that the timer or blocklist request failed.
- **Still open (not part of the blocklist milestone):** recheck the explicit live Radarr flag; reassess the 32 historical success rows created under v1.3.3 without resetting SQLite or resurrecting intentionally removed hashes; and confirm downstream `torr2strm` processing. `FileStats` alone does not prove successful playback or end-to-end library ingestion.
## Implemented

- One standard-library Python implementation for both Radarr and Sonarr.
- One systemd service/timer instance per configured Arr instance.
- Polling the paginated Arr /api/v3/history endpoint on each run.
- Filtering to usable Grabbed records and normalizing/deduplicating their BitTorrent infohashes.
- Retrieving the magnet or direct torrent URL through the history data.downloadUrl, including resolution of HTTP redirects.
- Inspecting new and unresolved TorrServer hashes through exact-hash POST /torrents with {"action":"get","hash":"..."}; ordinary and rebuild processing no longer snapshot the full torrent inventory.
- Adding through TorrServer POST /torrents with title, movie/tv category, safe custom data JSON and save_to_db: true.
- Fetching Radarr movie or Sonarr series details to obtain trustworthy provider IDs when they are missing from history.
- Shared SQLite success history across all Arr instances, plus a pending-add table for restart-safe asynchronous verification.
- Per-instance lock files, retry handling for transient HTTP/network failures and redaction of API-key-like URL parameters.
- --dry-run and --rebuild.
- Protection against automatic re-adding after a user removes a known imported torrent from TorrServer.
- Useful summary logs (HISTORY, SNAPSHOT, ADD, ADD_ACCEPTED, ADDED, VERIFY_FAILED, FAILED, DONE).
- Batch verification with per-hash deadlines and shared polling. Installed v1.3.4 requires usable `FileStats`, validates IDs/paths, inspects live incomplete objects without duplicate adds, and supports bounded metadata activation for DB-only records. GitHub Actions passed; live readiness and terminal blocklist paths were confirmed on MINI-Ubuntu on 2026-10-29. Historical v1.3.3 success rows and downstream torr2strm confirmation remain open in item 7 below.
- Atomic pending claims across Arr instances, persistent retry state, bounded exponential backoff and explicit --retry-failed recovery.
- Terminal-only grouped critical Apprise messages with persistent delivery retry state, independent of TorrServer retry state.
- Optional Seerr Watchlist tracking for terminal transfer failures.
- Independently configured Radarr and Sonarr native blocklist handling after the configured add/verification attempt limit is exhausted. Both paths are live-confirmed on MINI-Ubuntu; see item 10 below.
- Stale pending-row cleanup when the hash is already in successful import history, and re-arming after a strictly newer Grab for the same hash.

## v1.4.1 acceptance contract: hash-addressed in-run verification

**Status: implementation committed to the v1.4.1 candidate branch and production-manually validated on MINI-Ubuntu on 2026-10-31.** GitHub Actions run [#157](https://github.com/evgendman/arr2torr/actions/runs/41210488097) passed on Python 3.11, 3.12 and 3.14 with 61 tests per version, compilation and version check. Both isolated API smoke tests passed; production source identity, backup, in-place migration, Radarr and Sonarr manual runs, and final SQLite integrity/count checks are recorded in [the production validation report](docs/VALIDATION-v1.4.1-MINI-Ubuntu-2026-10-31.md). Representative CPU/request-count comparisons and a v1.3.5 binary A/B comparison remain outstanding. The runtime is installed but unreleased. Both 1080p timers are confirmed active (waiting); the 4K timers remain untouched.

### Goals and agreed design

- Remove `POST /torrents {"action":"list"}` from the ordinary importer workflow. The importer must not build a full TorrServer inventory to process a small number of hashes. Use address-specific `POST /torrents {"action":"get","hash":"..."}` only for new candidates and unresolved pending hashes.
- Apply the same hash-addressed lookup principle to deliberate `--rebuild` processing: inspect only hashes being reconsidered; do not fetch the complete TorrServer list.
- Do not call TorrServer at all when the current Arr history produces no new eligible hashes and the shared SQLite database contains no unresolved work due for a check. Arr history polling remains the discovery mechanism; this item does not introduce an Arr webhook or stop polling Arr.
- Check the response to each `action=add` immediately. If it already contains usable `FileStats` for the exact expected hash, confirm immediately; otherwise retain the hash as pending and add it to the current run's verification queue.
- After the initial pass over existing pending hashes and all new candidates, run one shared `action=get` carousel for every unresolved hash. Poll at the configured interval, remove each hash immediately on valid `FileStats`, and enforce an independent 120-second default deadline per hash.
- Disable automatic HTTP-client retries for the side-effecting TorrServer `action=add` request (`retry_count=0`). Persist an uncertain outcome and resolve it through later hash-addressed checks and the existing SQLite retry/backoff state machine; retain bounded retries for read-only requests where appropriate.
- Preserve the shared SQLite database, atomic pending claims and coordination across all four possible Radarr/Sonarr 1080p/4K instances. A hash claimed by one instance must not be added concurrently by another.
- Preserve the successful-import/removal safeguard: once a hash has been confirmed and recorded in `imports`, ordinary runs must not query TorrServer for it or resurrect it if a user later removes it. `--rebuild` remains the explicit operator-controlled exception.
- **Successful import continues to mean only that usable `FileStats` are available for the exact hash.** An HTTP 200 response, an echoed hash, list/get membership, or a status string such as `Torrent added`, `Torrent working` or `Torrent in db` is not sufficient. Retain the existing checks for non-empty file structure, positive unique file IDs, unique paths and safe relative paths.

### Proposed per-hash processing

1. Read and deduplicate the configured Arr history as today. Form candidates using the current shared import history and pending state.
2. If there are no new candidates, due retries or pending hashes requiring observation, finish without making any TorrServer request.
3. Query that exact hash with `action=get`. Concurrent Arr instances may perform a duplicate read, but the shared database claim must still ensure that only one can submit an add.
4. Handle the result:
   - If usable `FileStats` are returned, record the ready hash in shared `imports`.
   - If the object exists but its file structure is incomplete, atomically reserve it in `pending_adds` and queue it for the shared in-run carousel.
   - If it is absent, prepare the required media/link data, atomically claim the hash, then submit one `action=add` request.
   - If the lookup fails because of a network or server error, do not add or count an attempt; queue a read-only retry in the current run.
5. Validate the immediate `action=add` response. Confirm only when the exact hash has usable `FileStats`; otherwise retain the accepted/uncertain pending state and enqueue it for in-run polling.
6. Once all candidates have had their initial check/add decision, sweep the shared queue with exact-hash `action=get` requests at `verification_poll_interval_sec`. Remove a hash on success. Each queue entry has its own `verification_timeout_sec` deadline (default 120 seconds); an expired entry remains pending for the next timer run without a failed-add increment.
7. Keep add-attempt counting separate from the number of `get` observations. `max_add_attempts` limits actual `add` submissions, not read-only checks.

### Pending, DB-only and ambiguous absence handling

- Use `verification_timeout_sec` as an individual in-run wait limit for each queued hash (default 120 seconds). Expiration ends only that hash's current carousel participation; it remains pending for the next importer invocation and does not itself become a failed add.
- A present-but-incomplete torrent remains pending; do not resend `add` while a live object is observable.
- TorrServer MatriX.145 `action=get` calls `GetTorrent()`. If the hash is present only in TorrServer's database, that path may begin reloading the torrent into memory. Treat the initial `Torrent in db` response as incomplete, not as success; allow subsequent `get` observations to detect when usable `FileStats` become available. Do not add an unnecessary duplicate activation request merely because the first response is DB-only.
- A 404 after an earlier accepted `add` is inherently ambiguous: the torrent may have failed to initialize, or the user may have deliberately removed it before the next timer run. Do not blindly resend `add` immediately. Require two missing-hash observations on separate importer runs before entering the existing bounded retry/backoff policy. A 404 during the same run in which `add` was submitted is not counted toward that threshold. API polling alone cannot perfectly distinguish these two causes.
- Network errors and 5xx responses from `get` are unknown observations, not missing-torrent evidence. They must not consume the add-attempt counter.
- Preserve terminal failure, native Radarr/Sonarr blocklisting, Seerr Watchlist and grouped notification behavior. Their triggers must continue to depend on the actual terminal add/verification outcome, not merely on a delayed or failed read.

### Implementation and validation checklist

- [x] Remove full `action=list` snapshots from ordinary processing and rebuild candidate handling; replace them with exact-hash `action=get` checks.
- [x] Skip all TorrServer requests when there are no new eligible candidates and no unresolved pending work requiring observation.
- [x] Preserve atomic shared pending claims and prove that concurrent Radarr/Sonarr instances cannot submit the same hash twice.
- [x] Validate usable `FileStats` in the immediate `action=add` response and confirm immediately only when the full existing safety contract passes.
- [x] Disable automatic HTTP retries for `action=add`; retain SQLite-backed bounded retries and uncertain-outcome recovery.
- [x] Implement one shared in-run `action=get` carousel for all unresolved hashes, with immediate removal on valid `FileStats` and an independent 120-second default deadline per hash.
- [x] Implement hash-addressed pending verification, including incomplete live objects, DB-only records that are reloaded by `get`, 404 ambiguity, network errors and server errors.
- [x] Define and test a conservative policy for a 404 after an accepted add so a user removal is not immediately mistaken for a failed add and blindly re-added.
- [x] Keep the successful-import rule strict: only usable `FileStats` for the exact hash may create an `imports` record. Keep intentionally removed, previously confirmed hashes protected in normal mode.
- [x] Ensure `max_add_attempts` counts only actual `add` requests; read-only `get` checks do not consume attempts.
- [x] Preserve `--rebuild`, `--retry-failed`, terminal blocklisting, Seerr Watchlist, Apprise notifications and in-place SQLite migration semantics.
- [x] Add regression tests for immediate-ready add responses, shared multi-hash polling, immediate queue removal on success, independent per-hash timeout, pending preservation after timeout, exact-hash get, DB-only reactivation, absent hashes, in-run 404 exclusion from the two-run safeguard, ambiguous 404 across separate runs, transient read failures, deliberate user removal, rebuild and cross-instance concurrent claims. GitHub Actions run [#157](https://github.com/evgendman/arr2torr/actions/runs/41210488097) passed on Python 3.11, 3.12 and 3.14 with 61 tests per version.
- [ ] Measure request counts and CPU on MINI-Ubuntu under repeatable idle, one-pending-hash, small-batch and larger-library cases; compare against v1.3.5 using an accessible, verified copy. The 2026-10-31 one-request response-size sample is recorded above but is not this benchmark.
- [x] Run an isolated, read-only v1.4.1 smoke test against the real MINI-Ubuntu TorrServer API using temporary Arr/config/SQLite components and a proxy that blocks every action except exact-hash get; verify DB-only to usable-FileStats recovery. See the 2026-10-31 record above.
- [x] Complete separately authorized production importer validation on MINI-Ubuntu: install the source-pinned v1.4.1 executable, back up and migrate the shared SQLite database in place, run both Radarr and Sonarr manually, and verify the final database integrity/counts. See the 2026-10-31 production report above.
- [x] Re-enable only the Radarr/Sonarr 1080p timers after successful manual validation. On 2026-10-31 at 22:04:09 +05:00, both timers were confirmed `enabled` and `active (waiting)`, with their next trigger reported as 22:09:09 +05:00. The 4K timers were not touched.

### Scope boundary

This optimization does not move metadata lookup or release-selection work from Radarr/Sonarr into Python. The current per-run Arr media-detail cache remains; reusing already persisted metadata during retries can be considered as a separate low-risk improvement. Incremental Arr history pagination is also separate from this TorrServer optimization and must not be bundled into it without an independent justification.

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

**Historical observation (2026-10-21):** the then-installed torr-arr-importer version and source declared v1.1.0, with checkout commit **7831553da914b147d65989699b34a84c3ce238e5** recorded at that time. This section and the checks below are retained as a historical record only; the current installed v1.3.4 runtime, live evidence and timer caveats are documented at the top of this roadmap.

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

**Status: deployed and live-validated on MINI-Ubuntu on 2026-10-29.** Installed v1.3.5 requires usable `FileStats` rather than exact-hash membership alone. The installed importer file's Git blob SHA matches source (`712d6f4a276602250a25c52c5f5ccf6b0af92667`); 56 tests per Python version pass in GitHub Actions [run #139](https://github.com/evgendman/arr2torr/actions/runs/40844001781). Live runs confirmed 20 additions with usable `FileStats`, seven terminal failures reaching Radarr's native blocklist and five reaching Sonarr's native blocklist; grouped critical Apprise delivery returned HTTP 200. The 32-row v1.3.3 historical re-assessment and downstream torr2strm confirmation remain open.

**Required success contract**

The immediate `POST /torrents` response is only an acknowledgement that the request was accepted. Seeing the exact hash in `action=list` is also only an intermediate presence signal, not final success. Additions must remain pending until TorrServer exposes a usable, non-empty `FileStats` list for the exact hash. Entries must have positive unique file IDs and unique, safe relative paths sufficient for downstream tree generation. Do not infer success from `Torrent added`, `Torrent getting info`, `Torrent working`, or `Torrent in db` alone; only usable `FileStats` satisfy the downstream contract.

When a hash is listed without usable `FileStats`, keep checking its details within the configured deadline. For a DB-only `Torrent in db` record, explicitly trigger/continue metadata activation through the supported TorrServer path and then poll for usable `FileStats`; do not count the DB record alone as success. Do not blindly issue duplicate add requests while a live object still exists. If the deadline expires without usable `FileStats`, do not promote the hash into successful-import history; persist the unresolved attempt and use the bounded retry policy. A retry must distinguish an absent hash (eligible for another add) from a present but metadata-incomplete hash (requires metadata recovery/re-observation). Keep the existing intentional-removal safeguard and terminal failure/blocklist/notification behavior.

- [x] Treat the immediate add response as an acknowledgement, not final success.
- [x] Submit eligible candidates first and use shared polling with individual deadlines.
- [x] Make exact-hash presence in `action=list` an intermediate signal only; require usable `FileStats` before promoting a new or pending item to successful-import history.
- [x] Handle `Torrent added` / `Torrent getting info` as incomplete states; do not accept `Torrent working` or `Torrent in db` alone without usable `FileStats`.
- [x] Implement and test the metadata-recovery path for DB-only/present-but-incomplete hashes without blindly resending adds to live objects.
- [x] Preserve bounded retries and terminal-failure handling when `FileStats` never arrives; an unverified pending item is not promoted to successful-import history.
- [x] Add regression tests for hash-visible-without-FileStats, delayed readiness, malformed metadata, metadata-recovery `action=get` HTTP 404, DB-only activation and absent-versus-present retry handling.
- [x] Live-validate on MINI-Ubuntu that newly added releases are recorded as successful only after usable `FileStats`; 20 successful additions were observed in the 2026-10-29 logs.
- [ ] Confirm at least one of those verified releases completed downstream processing by `torr2strm`; `FileStats` alone does not prove end-to-end library ingestion.
- [ ] Reassess the 2026-10-29 batch of 32 v1.3.3 historical rows against usable `FileStats`; the earlier 32/32 list-presence result is not sufficient evidence of downstream readiness.
- [x] Live-validate terminal failure after three attempts, Radarr's native blocklist action, and the grouped critical Apprise outcome. Seven release choices were blocklisted and the grouped critical deliveries received HTTP 200.

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

### 10. Automatically blocklist terminally failed Radarr/Sonarr releases

**Status: completed — both Radarr and Sonarr native blocklist paths are live-confirmed in v1.3.5 on MINI-Ubuntu (2026-10-29).** Seven terminal film-release choices reached Radarr's native failed-history path and logged `RADARR_BLOCKLIST_BLOCKED`; five terminal series-release choices logged `SONARR_BLOCKLIST_BLOCKED`. For Radarr, five replacement releases were verified with usable `FileStats`; for Sonarr, replacement additions for Outer Banks S01, Outer Banks S03 and Dark S03 were verified. No replacement additions for Love, Death & Robots S02/S03 were observed in the reviewed log window; replacement search depends on Sonarr's own settings and available releases. This does not negate the successful blocklist actions. GitHub Actions run #139 passed on Python 3.11/3.12 with 56 tests per version plus compilation and version check; no v1.3.5 tag or GitHub Release has been published yet.

**Goal**

Reduce manual intervention by excluding a release from future Arr selection after arr2torr exhausts TorrServer add/verification attempts, allowing the respective application's native failed-download handling to search for a workable alternative according to its settings.

**Configuration and scope**

- Include `radarr_blocklist_enabled = true` explicitly in every Radarr example and active Radarr config; include `sonarr_blocklist_enabled = true` explicitly in every Sonarr example and in each active Sonarr config that should use the feature.
- Each flag defaults to `true` when missing for backward compatibility; explicit settings are recommended. Set either flag to `false` only as a deliberate opt-out for that instance.
- Use the native `POST /api/v3/history/failed/{historyId}` route for the exact Grab history record. Do not invent a direct POST to `/api/v3/blocklist`.
- The native action invokes the corresponding Arr app's ordinary failure pipeline; replacement searching follows that app's settings.

**Trigger and safety rules**

- Process only `pending_adds` rows with terminal status = `failed` and `attempt_count >= max_add_attempts` after a TorrServer add/verification attempt. Do not act on `retry_wait`, before the configured count is exhausted, or on permanent preparation errors that happened before the TorrServer add.
- Preserve the TorrServer attempt/backoff policy. The blocklist action must not mark an unverified torrent as a successful TorrServer import.
- Store separate Radarr/Sonarr status, error and verification-counter columns in the existing SQLite database; add columns in place and never reset shared state.
- On each feature's first migration, mark already-failed rows as `legacy_skipped` so upgrades do not trigger retrospective batches of failed-history actions. Explicit `--retry-failed` resets the corresponding state for a deliberately re-armed attempt.
- Persist `request_started` before the external side effect. If the result is ambiguous, verify read-only on later runs and never blindly repeat the failed-history POST.
- Radarr recovery checks `GET /api/v3/blocklist/movie?movieId=...`. Sonarr recovery checks `GET /api/v3/blocklist?seriesIds=...` and reads its paginated `records` array. Both compare source release title because their public blocklist resources do not expose the source torrent's exact infohash; this is not exact-hash proof.

**Apprise notifications and logging**

- Append the relevant blocklist outcome to the existing grouped terminal-failure message; do not send a separate blocklist-only message.
- Report confirmed native-action success, a matching source-title result during recovery, an ambiguous state or a definitive failure with a sanitized reason. Do not mention blocklisting when the matching flag is disabled or the failure happened before a TorrServer add.
- Use `RADARR_BLOCKLIST_*` and `SONARR_BLOCKLIST_*` log prefixes, including the Arr instance and relevant media/history IDs. Never log API credentials.
- Preserve terminal notification severity, aggregation, deduplication and delivery retries.

**Implementation checklist**

- [x] Inspect and document the native failed-history route for both Arr apps; use their native failure pipeline rather than a direct blocklist-creation POST.
- [x] Add independent per-instance flags and explicit `true` values in Radarr and Sonarr example TOMLs.
- [x] Restrict actions to terminal TorrServer add/verification failures after the configured attempt limit.
- [x] Add separate in-place SQLite state migration; mark pre-upgrade failures `legacy_skipped`.
- [x] Add separate log outcomes and append blocklist status to the existing terminal Apprise message.
- [x] Add Sonarr regression coverage for config defaults/disablement, native endpoint, paginated records, ambiguous-result recovery and notification wording.
- [x] Preserve the existing live-validated Radarr flow and its test coverage.
- [x] Run CI for v1.3.5 on supported Python versions (GitHub Actions run #139: 56 tests each on Python 3.11 and 3.12, plus compilation and version check).
- [x] Live-validate Sonarr's terminal-failure path on MINI-Ubuntu. Five failed series-release choices logged `SONARR_BLOCKLIST_BLOCKED`; replacement additions for Outer Banks S01, Outer Banks S03 and Dark S03 passed usable-`FileStats` verification. Unit tests cover ambiguous-response recovery. No alternative additions for Love, Death & Robots S02/S03 were observed in the reviewed log window; replacement availability remains dependent on Sonarr's settings and available releases.


## Explicitly not planned

- A webhook listener: polling the Arr history is the selected design.
- A Blackhole directory watcher: Blackhole permits Arr's Grab workflow, but the importer uses the Arr API and TorrServer API.
- Downloading media inside the importer: TorrServer is the receiving/streaming service.
- Silently resurrecting user-removed torrents during normal polling: rebuild must remain an explicit action.
