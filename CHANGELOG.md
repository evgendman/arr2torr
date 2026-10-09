# Changelog

## torr-arr-importer v1.0.3

- Clarified the history summary counters: unique_hashes and duplicates replace the ambiguous valid count.
- Removed a duplicate data assignment.
- Updated the default User-Agent and version string to 1.0.3.
- Represents the current version used for the project documentation.

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
