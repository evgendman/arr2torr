# Changelog

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
