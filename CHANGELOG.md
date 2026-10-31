# Changelog

## torr-arr-importer v1.4.1 — installed and production-live-validated on MINI-Ubuntu (2026-10-31); not released

- Restore in-run verification after the initial candidate pass, using one shared carousel of exact-hash TorrServer `action=get` requests for all unresolved candidates.
- Poll at the configured `verification_poll_interval_sec`; each candidate has its own `verification_timeout_sec` deadline (default 120 seconds), measured from queue entry. Successfully verified hashes leave the queue immediately.
- Keep candidates pending when their individual wait expires. A timeout ends only the current run's wait and does not increment `attempt_count`, trigger blocklisting or add a separate metadata-failure counter.
- Count only actual `action=add` submissions against `max_add_attempts`. Repeated in-run 404s are not treated as separate timer-run observations; the existing two-run missing-hash safeguard remains.
- Retain exact-hash reads, the shared SQLite state machine, atomic claims, no-full-inventory behavior and add-request retry protection.
- **CI:** GitHub Actions [run #157](https://github.com/evgendman/arr2torr/actions/runs/41210488097) passed on Python 3.11, 3.12 and 3.14 with 61 tests per version, compilation and version check.
- **Runtime identity:** installed at `/opt/torr-arr-importer/importer.py` on MINI-Ubuntu; `--version` reports `1.4.1`; Git blob SHA `c9943d92a396882765a92e01cb40d28aa7388777` matches the candidate source. The previous v1.4.0 executable was backed up.
- **SQLite migration:** verified pre-migration SQLite backup was saved with SHA-256 `c701a4a5a953f12b59a25b183a54bf1963538fb0126b398fe9200f3661ab6dda`. The in-place migration added only `pending_adds.missing_observations`; no tables or rows were reset.
- **Radarr live run:** the DB-only Groundhog Day (1993) entry became ready through the shared in-run exact-hash `action=get` carousel. Summary: submitted=0, added=1, failed=0, pending=22; duration=4.03 s. No duplicate add was submitted.
- **Sonarr live run:** summary submitted=0, added=0, failed=0, pending=22; duration=5.31 s.
- **Final production database:** `PRAGMA integrity_check` returned `ok`; imports=729; pending_adds=22 (Radarr failed=7, Sonarr failed=15); the Groundhog Day hash is present once in `imports` and absent from `pending_adds`.
- Detailed dry-run counters, re-arm audit, trace paths and service-state caveats are recorded in [the MINI-Ubuntu production validation report](docs/VALIDATION-v1.4.1-MINI-Ubuntu-2026-10-31.md) and [ROADMAP.md](ROADMAP.md).
- **Still outstanding:** controlled CPU/request-count comparisons across idle, pending, small-batch and larger-library workloads, and an executable A/B comparison against v1.3.5. Timer reactivation is a separate operator step and is not claimed until systemd confirms it.
- **Publication:** validation is successful, but PR #6 remains draft; no v1.4.1 tag or GitHub Release has been published.

## torr-arr-importer v1.4.0 — candidate, not deployed

- Replace full TorrServer `action=list` snapshots in ordinary operation with hash-addressed `action=get` checks for new and unresolved torrents. A confirmed hash is recorded in shared SQLite verified-import history, so ordinary runs do not query it again.
- Skip all TorrServer API requests when the current run has no new eligible hashes and no unresolved pending hashes that need observation. Arr history remains the source of new Grab discovery.
- Validate the immediate `action=add` response. If it contains usable `FileStats` for the exact hash, confirm the import immediately; otherwise persist a pending attempt and return without waiting for metadata acquisition.
- Remove the in-process verification polling loop and the 120-second synchronous wait. Unresolved hashes are checked by `action=get` during later systemd timer runs.
- Separate atomic hash reservation from the add-attempt counter. Claims and read-only `get` requests do not consume `max_add_attempts`; the counter advances only when an actual `action=add` is submitted.
- Disable automatic HTTP retries for TorrServer `action=add`. Ambiguous add outcomes are reconciled by exact-hash reads on later runs.
- Require two separate timer-run 404 observations after an earlier accepted add before moving the operation into the bounded retry/backoff path. A 404 on a due retry can authorize the next add; network errors and 5xx responses do not count as absence.
- Treat DB-only `Torrent in db` responses as incomplete. MatriX.145 `action=get` can trigger a database-backed torrent reload; the importer observes it on later runs instead of sending a duplicate metadata-activation `add`.
- Preserve the strict success contract: only non-empty, safe `FileStats` for the exact hash can enter successful-import history. Preserve shared SQLite coordination, deliberate-removal protection, rebuild/retry commands, blocklisting, Seerr Watchlist and notifications.
- Add an in-place `missing_observations` migration to track consecutive 404 observations without deleting or resetting the existing shared state database.
- Add regression coverage for immediate readiness, deferred verification, empty cycles, exact-hash reads, present/incomplete and DB-only entries, ambiguous 404 handling, retry counters, rebuild and cross-instance claims.
- **Validation status:** candidate branch only. GitHub Actions run #149 passed on Python 3.11/3.12 with 59 tests each, compilation and version check. Comparative CPU/request measurements and MINI-Ubuntu live validation remain before deployment; no runtime update is implied by this changelog entry.

## torr-arr-importer v1.3.5 — deployed and live-validated (2026-10-29)

- Add explicit `sonarr_blocklist_enabled = true` to both Sonarr example TOMLs and parse the setting independently of Radarr's existing flag.
- After a Sonarr release reaches terminal TorrServer add/verification failure and exhausts `max_add_attempts`, use Sonarr's native `POST /api/v3/history/failed/{historyId}` route. Preserve bounded TorrServer retries and exclude pre-add preparation errors.
- Persist Sonarr blocklist state in dedicated SQLite columns, migrated in place; mark pre-upgrade failed Sonarr rows as `legacy_skipped`, and avoid resending a non-idempotent failed-history request after ambiguous outcomes.
- Verify an ambiguous Sonarr action using `GET /api/v3/blocklist?seriesIds=...` and source-title matching; the public API does not prove an exact infohash match.
- Add regression tests for enabled/disabled config, native endpoint use, separate state, paged blocklist parsing, ambiguous-response recovery and terminal notification wording.
- **CI:** GitHub Actions [run #139](https://github.com/evgendman/arr2torr/actions/runs/40844001781) passed on Python 3.11 and 3.12 with 56 unit tests per version, compilation and version check (`1.3.5`).
- **Runtime identity:** `/opt/torr-arr-importer/importer.py --version` returned `1.3.5` on MINI-Ubuntu. The installed file matches the source file's Git blob SHA (`712d6f4a276602250a25c52c5f5ccf6b0af92667`).
- **Radarr live validation:** seven terminally failed release choices reached the native failed-history action and logged `RADARR_BLOCKLIST_BLOCKED`; three grouped critical Apprise notifications were acknowledged with HTTP 200. Five alternative releases passed `FileStats` verification.
- **Sonarr live validation:** five terminally failed series-release choices logged `SONARR_BLOCKLIST_BLOCKED`. Replacement additions for Outer Banks S01, Outer Banks S03 and Dark S03 then passed `FileStats` verification. No replacement additions for Love, Death & Robots S02/S03 were observed in the reviewed log window; that is a replacement-search/result observation, not a failure of the blocklist action.
- **Publication status:** runtime and blocklist behavior are validated; PR #4 remains open, and no v1.3.5 Git tag or GitHub Release has been published.

## torr-arr-importer v1.3.4 — strict FileStats verification base (superseded by v1.3.5)

- Configuration convention: explicitly set `radarr_blocklist_enabled = true` in each active Radarr TOML. The code fallback to `true` when the key is absent remains only for backward compatibility; the setting is visible in both Radarr example configs. Sonarr configs do not use this Radarr-only setting.

- Require a usable, non-empty TorrServer `FileStats` list for the exact infohash before confirming a successful import. Validate positive unique file IDs and unique safe relative paths.
- Treat the add response and `action=list` hash membership as intermediate signals only; `Torrent added`, `Torrent getting info`, `Torrent working` and `Torrent in db` are not sufficient success evidence by themselves.
- Inspect present-but-incomplete live torrents via `action=get` without blindly resending `action=add`. For DB-only records, allow bounded metadata activation no more than once per verification attempt, then continue polling for usable `FileStats`.
- Keep absent-hash retries separate from present-but-incomplete metadata recovery. Add a SQLite marker for metadata activation and migrate it in place; do not reset existing import history or deployment state.
- Add regression coverage for malformed/missing `FileStats`, delayed readiness, DB-only activation, live incomplete hashes, absent-versus-present retry handling, and `action=get` HTTP 404 behavior.
- GitHub Actions [run #117](https://github.com/evgendman/arr2torr/actions/runs/40833440612) passed on Python 3.11 and 3.12 (49 unit tests, compile step and version check; reported version `1.3.4`). The code and tests are unchanged from the source pin installed on MINI-Ubuntu.
- **Live verification on MINI-Ubuntu (2026-10-29):** the installed program reports `1.3.4`; SHA-256 `cb10e78ab5f9e94d36af5cb77622ad86601a505bb1410f85bbe458d3dd82c3f4` matches source commit `78ba4801e5e1fc3cfdc4cb620e2e55853e61573c`. Twenty new/replacement additions were observed reaching `ADDED verification=usable-file-stats`.
- Seven terminally failed release choices reached Radarr's native failed-history action and logged `RADARR_BLOCKLIST_BLOCKED`: *Leaves of Grass*, *Keeping the Faith*, *The French Dispatch*, *Motherless Brooklyn*, *Isle of Dogs*, and two *Sausage Party* releases. Three grouped critical Apprise sends were acknowledged with HTTP 200, for batches of two, four and one item. Five alternate releases subsequently passed `FileStats` verification; the *Sausage Party* HDRip 480p alternative also exhausted three attempts and was blocklisted, but no working replacement had appeared by the 13:05:54 run.
- The existing SQLite history was retained. The 32 historical rows accepted by v1.3.3's weaker list-only criterion have not been automatically reclassified.
- Superseded by v1.3.5 on 2026-10-29; current runtime identity and live validation are recorded in the top changelog entry.

## torr-arr-importer v1.3.3 (installed from PR #4 branch for controlled live validation; PR remains open)

- Treat exact infohash membership in TorrServer's list as successful addition; remove the readiness-metadata requirement while retaining the requirement for a post-add list observation.
- Remove stale pending_adds rows when the same hash is already in the successful imports history, without re-adding a hash that is absent from TorrServer after a deliberate removal.
- Re-arm an exhausted failed row only when Radarr presents a strictly newer Grab history ID for the same hash; reset attempt, notification, Seerr and blocklist state for that new Grab.
- Add regression tests for list-presence confirmation, stale pending cleanup, and re-grabbing a terminally failed hash.
- Install the v1.3.3 code-only update on MINI-Ubuntu without changing TOML configuration, SQLite state or systemd units.
- Live log review for the 10-hour window ending 2026-10-29 09:45 (+05:00): 115 completed Radarr runs, 79/79 add requests confirmed by subsequent list membership, and zero failed additions or importer ERROR/exception events.
- The zero-seed Viking Quest 720p release appeared in TorrServer's list on the first attempt. This did not trigger retries or Radarr blocklisting; the live terminal-failure/blocklist path remains live-validation work.

## torr-arr-importer v1.3.2 (combined candidate)

### Radarr terminal-failure blocklisting

- Enable Radarr blocklisting by default (opt-out with radarr_blocklist_enabled = false) only after an add/verification attempt has reached terminal failure after the configured retry limit.
- Use Radarr's supported POST /api/v3/history/failed/{historyId} endpoint so Radarr's native failure pipeline records the Grabbed release in its blocklist.
- Keep Sonarr behavior and pre-add preparation failures unchanged. Preserve bounded TorrServer retries and do not issue duplicate failed-history requests after an ambiguous response.
- Add an in-place SQLite state migration, explicitly skip pre-upgrade terminal failures to avoid retroactive mass blocklisting, and reset the state when --retry-failed re-arms an item.
- Append the blocklist outcome to the existing grouped terminal Apprise failure message and add structured logs without introducing a separate notification.

### Terminal-only notifications and durable delivery

- Suppress user-facing messages during retry-wait states. Notify only when add/verification reaches terminal failure or a permanent preparation error prevents the add.
- Send one aggregated critical/failure message with media/release titles, a plain-language cause, attempts used or permanent-failure context, a recommendation to choose another release, the Arr card link when available, the Seerr Watchlist result, and the Radarr blocklist outcome where applicable.
- Remove separate retry-warning and Seerr Watchlist notifications so one transfer failure is not announced through multiple topics.
- Translate common causes into concise Russian text while retaining technical detail in local logs.
- Persist notification delivery attempts, last error and next retry time in SQLite. Mark terminal items notified only after an Apprise 2xx response; retry failed deliveries with a persisted backoff capped at one hour.
- Keep delivery retries independent from TorrServer add attempts and import status. Network/notification failures do not change transfer success/failure semantics.
- Keep distinct terminal-error rows separate when TMDb IDs are missing and test grouping, message aggregation, silent retry states and retry after Apprise failure.
- SQLite changes are additive and migrate in place. This combined behavior is installed in the v1.3.3 test candidate; the real terminal-failure-to-Radarr-blocklist path remains live-validation work.

## torr-arr-importer v1.3.1

- Fix a startup race when Radarr and Sonarr initialize the shared SQLite database concurrently. Schema migrations now acquire a write reservation before inspecting and altering table schemas.
- Add a concurrent-initializer regression test for in-place migration of an existing v1.2 database. No state database reset or data deletion is required.

## torr-arr-importer v1.3.0

- Add an opt-in Seerr Watchlist integration for terminal TorrServer transfer/verification failures; it creates a Watchlist attention marker only and does not create a media request or call Radarr/Sonarr.
- Use the existing SQLite state DB and timers. Store separate Watchlist status/notification fields and migrate older databases in place without clearing import history.
- Persist TMDb/TVDB IDs for permanent preparation failures when already available in Arr metadata; do not perform additional external ID lookups when an ID is missing.
- Send grouped Apprise `warnings` notifications when a failed item is registered (or verified already present) in the administrator's Seerr Watchlist; send grouped `critical` failures when registration cannot be completed.
- Add duplicate verification for Seerr HTTP 409, retries for transient API failures, deduplication of unchanged alerts, and API-key redaction.
- Add tests for movie/series payloads, terminal failure selection, missing IDs, duplicate confirmation, retries, notification routing and state migration.

## torr-arr-importer v1.2.0

- Add opt-in Apprise API notifications using the shared `arr-stack` configuration and tag-based routing; no direct Telegram credentials are stored by the importer.
- Send numeric-only, aggregated warnings when the retry-wait count/attempt distribution changes; suppress unchanged messages across timer runs.
- Send one grouped critical notification for newly terminal add/verification failures and permanent HTTP 4xx preparation failures, with human-readable media/release titles, concise sanitized causes, and optional Arr UI links.
- Persist notification state in the existing SQLite database; suppress historical failed rows during migration, avoid endless delivery retries, and keep notification outages from affecting imports.
- Add tests for aggregation, deduplication, terminal-failure details, link generation, redaction, and unavailable/disabled Apprise.

## torr-arr-importer v1.1.0

- Do not record a successful import solely because TorrServer returned HTTP success and the expected infohash.
- Persist pending add attempts in shared SQLite before the side-effecting request and reconcile them after process/timer restarts.
- Submit eligible releases first, then verify all pending hashes with a shared polling loop and individual deadlines.
- Confirm observable readiness through usable FileStats, working status, or explicit TorrServer database-presence status.
- Add bounded exponential retry backoff, configurable attempt limits, and --retry-failed for explicit re-queueing of exhausted, never-confirmed attempts.
- Keep unverified attempts separate from confirmed import history and preserve intentional-removal suppression behavior.
- Add regression coverage for readiness evidence, pending/success transitions, retry limits, batch polling, restart recovery, and shared pending claims.

## torr-arr-importer v1.0.3

- Clarified the history summary counters: unique_hashes and duplicates replace the ambiguous valid count.
- Removed a duplicate data assignment.
- Updated the default User-Agent and version string to 1.0.3.
- Historical baseline for the core history, metadata and retry handling; superseded as the current code version by v1.1.0.

## v1.0.2

- Do not retry permanent HTTP 4xx errors such as 401/403.
- Retry transient HTTP statuses (408, 429, 5xx) and network failures.
- Redact API-key-like query parameters from URL messages in logs and exceptions.
- Use per-instance lock files while allowing all instances to share SQLite import history.
- Cache media detail responses by Arr media ID within each run.
- Improve history diagnostics and installation permissions for state/log storage.

## v1.0.1 and v1

- Established the polling design from Arr Grabbed history to TorrServer.
- Added shared persistent import history and the --rebuild escape hatch.
- Added the initial systemd oneshot service/timer deployment pattern.
