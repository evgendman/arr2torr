# Changelog

## torr-arr-importer v1.3.4 (FileStats verification candidate; PR #4 remains open)

- Require a usable, non-empty TorrServer `FileStats` list for the exact infohash before confirming a successful import. Validate positive unique file IDs and unique safe relative paths.
- Treat the add response and `action=list` hash membership as intermediate signals only; `Torrent added`, `Torrent getting info`, `Torrent working` and `Torrent in db` are not sufficient success evidence by themselves.
- Inspect present-but-incomplete live torrents via `action=get` without blindly resending `action=add`. For DB-only records, allow bounded metadata activation no more than once per verification attempt, then continue polling for usable `FileStats`.
- Keep absent-hash retries separate from present-but-incomplete metadata recovery. Add a SQLite marker for metadata activation and migrate it in place; do not reset existing import history or deployment state.
- Add regression coverage for malformed/missing `FileStats`, delayed readiness, DB-only activation, live incomplete hashes, absent-versus-present retry handling, and `action=get` HTTP 404 behavior.
- GitHub Actions [run #103](https://github.com/evgendman/arr2torr/actions/runs/40830665619) passed on Python 3.11 and 3.12 (49 unit tests, compile step and version check; reported version `1.3.4`).
- This is a code candidate on PR #4, not a tagged release or live deployment. MINI-Ubuntu remains on v1.3.3 until separately authorized and live-validated.

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
