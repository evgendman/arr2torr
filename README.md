# arr2torr

**Move selected Radarr and Sonarr releases to TorrServer for streaming — without waiting for a full download.**

arr2torr is the project and documentation home for **torr-arr-importer**, a polling service that transfers releases selected with **Grab** in Radarr or Sonarr directly to the TorrServer API. It is intended for a media workflow where the user wants to start streaming from TorrServer, then expose those streams to a media library through STRM files or another integration.

**Installed on MINI-Ubuntu: torr-arr-importer v1.3.5 (live-validated 2026-10-29).** A separate **v1.4.0 candidate** is being prepared on branch `feature/nonblocking-hash-get-v1.4.0`; it is not deployed. The candidate replaces full TorrServer inventory polling with per-hash `action=get`, checks `FileStats` in the immediate add response and defers unresolved verification to later timer runs. See [Roadmap](ROADMAP.md) for implementation and validation status.

## Why this exists

Radarr and Sonarr are commonly configured for conventional downloading: a release is sent to a download client, the media is downloaded, and only then can it be imported into a library. That is a good model for a permanent local collection, but it is not always the desired experience.

TorrServer offers another route: it can open a torrent and stream the selected video without requiring the whole file to finish downloading first. The stream can be used directly, or surfaced in clients through a filesystem/web representation or STRM links.

The missing link is transferring the *chosen* release from an Arr application to TorrServer. torr-arr-importer fills that gap by reading the Arr history API and calling TorrServer's own API. It does not implement a second indexer, search for releases again, or download media files itself.

## The user experience

A typical workflow looks like this:

    User requests a film or series in Seerr
                   |
                   v
           Radarr or Sonarr
                   |
          user chooses a release
             and presses Grab
                   |
                   v
           Arr history records
             a Grabbed event
                   |
                   v
      torr-arr-importer timer runs
                   |
                   +---- reads Arr history
                   +---- resolves the original download URL
                   +---- adds the hash to TorrServer with metadata
                   |
                   v
               TorrServer
                   |
          +--------+---------+
          |                  |
     direct stream URL   STRM/media-library
                             |
                             v
                       Jellyfin/Kodi/etc.

With the usual five-minute timer, a successfully grabbed release is normally transferred on the next polling cycle. Playback still depends on TorrServer being reachable and on torrent metadata, peers and network conditions; this is not a guarantee that every torrent will start instantly.

## What the importer does

- Reads Radarr/Sonarr **history**, not their search results.
- Processes records whose event type is **Grabbed**.
- Identifies each torrent by its BitTorrent infohash (the history field data.torrentInfoHash).
- Handles the paginated /api/v3/history response and scans available history on each run, so it does not rely on a fragile “last processed timestamp”.
- Deduplicates repeated history records for the same infohash, keeping the newest record.
- Reads the current TorrServer torrent list and compares hashes before adding anything.
- Resolves the Arr history data.downloadUrl. In the tested Prowlarr setup, this URL redirects to the original magnet link.
- Adds the torrent through TorrServer's /torrents API, including the Arr title, media category and a safe subset of Arr metadata in data.
- Uses one shared SQLite database across all importer instances for verified imports and restart-safe pending attempts.
- Requires usable `FileStats` for the exact hash before recording a successful import; an HTTP acknowledgement or status string alone is not success.
- In the v1.4.0 candidate, atomically claims new hashes in shared SQLite before checking only those exact hashes with `action=get`; ordinary runs do not call `action=list`.
- Returns immediately after an add acknowledgement without waiting for metadata acquisition. Unresolved hashes remain in the shared SQLite pending state and are checked during later timer runs.
- Disables automatic HTTP retries for the side-effecting `action=add`; bounded retries are governed by the persistent SQLite state machine.
- Supports non-mutating --dry-run, deliberate --rebuild, and explicit --retry-failed for exhausted unverified attempts.
- For Radarr and Sonarr, can send terminal TorrServer add/verification failures to the respective native failed-download handling and blocklist once max_add_attempts is exhausted; independently configurable per instance.
- Uses separate per-instance configuration and lock files, while sharing the import-history database.
- Redacts API-key-like URL parameters from logs and exceptions.

## Asynchronous add verification (v1.4.0 candidate)

TorrServer may acknowledge an add request before its background metadata acquisition completes. The immediate add response is inspected for usable `FileStats`; only that exact-hash structure can confirm success. An HTTP 200 response, an echoed hash, or a status string such as `Torrent added`, `Torrent working` or `Torrent in db` is not sufficient.

The candidate persists a pending record and returns from the current run without a polling loop or a 120-second synchronous wait. Later systemd timer runs inspect only unresolved hashes through address-specific `POST /torrents` requests with `action=get`. The candidate no longer downloads the full TorrServer inventory with `action=list`. A DB-only `Torrent in db` response is incomplete; in TorrServer MatriX.145, `action=get` can start reloading that database-only torrent into memory, so the importer observes it on a later run rather than issuing a duplicate `add` activation request.

A new candidate is first checked by exact hash. If already present with usable `FileStats`, it is recorded in the shared verified-import history. If present but incomplete, it becomes pending and is observed on later runs. If absent, the importer prepares the metadata, atomically claims the hash in SQLite, and sends one `add`. The attempt counter increments only for an actual `add` submission, not for a `get` observation or a pending claim. Automatic HTTP-client retries are disabled for `add` because its outcome may be ambiguous after a network timeout.

Pending attempts survive process restarts and are reconciled across all configured Radarr/Sonarr instances through the shared SQLite database. A 404 after a previously accepted add is not retried immediately: two separate timer-run 404 observations are required before entering the existing bounded retry/backoff path. This reduces the chance of interpreting a brief disappearance or a user removal as an immediate add failure, although API polling cannot perfectly distinguish deliberate removal from failed initialization. Network errors and 5xx responses are unknown observations, not proof that a hash is absent.

Only usable, non-empty `FileStats` for the exact hash can create a successful-import record. Entries must have positive unique file IDs and unique safe relative paths. Confirmed hashes are not queried again during ordinary runs, preserving the safeguard against resurrecting torrents deliberately removed by the user. `--rebuild` remains the explicit operator-controlled exception. The v1.4.0 candidate still requires CI and live validation on MINI-Ubuntu; v1.3.5 remains the installed runtime.

## Final-failure Apprise notifications (introduced in v1.3.2; retained in v1.3.5)

Notifications are opt-in and use the shared Apprise API; arr2torr never stores Telegram tokens, chat IDs or topic IDs. **There are no Telegram messages while an item is still waiting for an automatic add/verification retry.** The importer waits until the failure is terminal (all configured add attempts are exhausted, or a permanent error makes retry impossible).

The final message goes to the critical tag and brings the entire action-oriented summary together: movie/series title, selected release, plain-language reason, attempts used, recommendation to choose another release in Radarr/Sonarr, and a direct Arr card link when available. When Seerr Watchlist integration is enabled, the same message reports whether the media was added, was already listed, or could not be registered. There is no second warning or Watchlist message for the same failure.

Terminal notification state is stored in the existing SQLite database and is migrated in place. An unsuccessful Apprise request leaves the message pending and schedules another attempt with increasing delays (1, 5, 15, 30 and then 60 minutes, capped at one hour). A successful 2xx response marks the notification delivered. This is at-least-once delivery: if Apprise accepts a request but its response is lost, a retry can result in a duplicate. Delivery failures never change the TorrServer import result. Notifications remain disabled in example configs by default. See [Installation and configuration](docs/INSTALLATION.md) and [Operations](docs/OPERATIONS.md).

## Automatic Radarr/Sonarr blocklisting after terminal transfer failure (v1.3.5)

Every Radarr example TOML explicitly sets `radarr_blocklist_enabled = true`, and every Sonarr example TOML explicitly sets `sonarr_blocklist_enabled = true`. Keep the matching flag explicit in each active config so the policy is visible without inspecting importer code. Both flags default to `true` for backward compatibility with existing configs; set a flag to `false` only to deliberately disable blocklisting for that instance.

Once a release reaches terminal TorrServer add/verification failure after all configured attempts, arr2torr calls the corresponding Arr application's native `POST /api/v3/history/failed/{historyId}` endpoint for that exact Grab history record. Radarr and Sonarr then use their own failed-download and blocklist pipeline; replacement searching depends on each application's settings.

- Intermediate failures waiting for retry are not blocklisted.
- Errors before the TorrServer add (such as an expired Grab download URL) are not treated as dead TorrServer releases.
- Radarr and Sonarr have independent flags and independent persisted state. Sonarr's flag is `sonarr_blocklist_enabled`; Radarr's is `radarr_blocklist_enabled`.
- State is saved in separate Radarr/Sonarr SQLite columns and migrated in place. Existing terminal failures are marked as pre-upgrade during the corresponding migration and are not sent to the Arr application retroactively.
- Outcomes are appended to the existing grouped terminal Apprise message and recorded in logs (`RADARR_BLOCKLIST_*` or `SONARR_BLOCKLIST_*`). There is no separate blocklist-only notification.
- If a failed-history request has an ambiguous HTTP/network result, the importer does not repeat the non-idempotent POST blindly. It checks the matching Arr blocklist on later runs and compares the release source title. Neither public blocklist resource exposes enough information to prove an exact infohash match in this recovery path.
- **Radarr:** seven terminally failed release choices reached `RADARR_BLOCKLIST_BLOCKED`; three grouped critical Apprise notifications were acknowledged with HTTP 200, and five replacement releases were verified.
- **Sonarr:** five terminally failed series-release choices reached `SONARR_BLOCKLIST_BLOCKED`; replacement releases for Outer Banks S01, Outer Banks S03 and Dark S03 then reached `ADDED verification=usable-file-stats`. No alternative addition for Love, Death & Robots S02/S03 was observed in the reviewed log window. This does not negate the confirmed blocklist actions: replacement search and available results depend on Sonarr's own configuration and the releases available to it.


## Seerr Watchlist for terminal transfer failures (v1.3.0)

The optional Seerr integration registers a movie or whole series in the administrator's personal Seerr Watchlist after a TorrServer transfer reaches terminal failure. It is an attention marker for manual review, **not** a Seerr media request: it does not ask Seerr to search/download the title and does not deliberately change Radarr/Sonarr monitoring or trigger their search commands.

- Controlled by `seerr_watchlist_enabled`, which defaults to `false`.
- Requires `seerr_url` and `seerr_api_key`. API-key requests use Seerr's administrator account (user ID 1) unless an explicit user override is sent; this integration does not send one.
- Reads terminal `failed` rows from `pending_adds` and permanent `terminal_events` for the current Arr instance. `pending` and `retry_wait` are ignored.
- Uses only the TMDb ID already stored in arr2torr. Missing IDs are recorded and reported; there is no separate metadata lookup.
- The Seerr registration result is included in the same final critical transfer-failure message; there is no separate warnings or Seerr notification. A successful or verified-existing Watchlist entry is described naturally, while registration errors are reported honestly and are retried by the integration where applicable.
- Watchlist processing state is stored in additional SQLite columns and is migrated in place. The import history and original TorrServer failure state remain intact.
- The Seerr Watchlist is used as an administrator reminder list for failed transfers; it does not create a Seerr media request.

The setting is off in all example configs until a real key is supplied and the operator enables it. Configure only the two active 1080p profiles unless the 4K instances are intentionally brought into use.

## What it deliberately does not do

arr2torr does **not**:

- download or seed the media itself;
- run a BitTorrent client or replace TorrServer;
- watch the Blackhole directory for .torrent or .magnet files;
- receive or require Arr webhooks;
- search Prowlarr/JacRed again after a Grab event;
- create STRM or NFO files itself;
- treat target_quality as a measurement of actual video resolution;
- automatically restore a torrent that the user deliberately removed from TorrServer during normal operation.

Those boundaries are intentional. The importer handles **selection-to-TorrServer transfer**. Media-file layout and STRM/NFO generation belong in the downstream library layer.

## Multiple Arr instances

A single Python program can serve as many Radarr/Sonarr instances as needed. A common setup has four independent Arr instances: 1080p and 4K versions of Radarr, plus 1080p and 4K versions of Sonarr.

Each instance has its own TOML configuration, systemd service/timer instance, log and process lock. All instances share the same state database:

    /var/lib/torr-arr-importer/state.db

That shared history makes the instances cooperate instead of duplicating work. If the same infohash appears in different Arr histories, instances coordinate through the shared pending-add table as well as the TorrServer inventory, reducing duplicate requests even when per-instance timers overlap.

The configured target_quality is stored as descriptive metadata from the source instance. It helps preserve which Arr lane selected the release, but it is **not** proof of the actual video resolution. A downstream STRM builder should inspect real stream metadata if the physical quality bucket must be accurate.

## How candidates are selected

For a normal run, define:

- **A** — distinct hashes in the source Arr's Grabbed history;
- **T** — hashes currently present in TorrServer;
- **I** — hashes in the successful import history.
- **P** — unresolved, retry-waiting, or exhausted attempts in the shared pending_adds table.
- **P_due** — pending retry-waiting attempts owned by this instance whose backoff has expired and whose attempt limit is not exhausted.
- **P_blocked** — all other pending attempts that must not be re-sent yet.

Normal candidates are effectively:

    Candidates = (A − T − I − P_blocked) ∪ P_due

Equivalently, due retries are admitted only after the usual Arr-history, TorrServer-membership and successful-import checks, and no other pending claim may be overwritten. The initial HTTP add response does not update I; only a later observable readiness/database-presence check promotes a pending hash into successful import history.

For an intentional historical resynchronization, --rebuild ignores **I** but still respects TorrServer membership and unresolved pending claims. A due, eligible pending retry remains subject to its attempt limit and backoff.

This is useful after switching to a fresh TorrServer database or when deliberately restoring all Grabbed items still present in Arr history. It does not erase the SQLite database; it changes the filtering rule for that run.

See [Architecture and state model](docs/ARCHITECTURE.md) and [Operations](docs/OPERATIONS.md) for the full logic.

## Blackhole: why it is configured, but not the transport

Radarr/Sonarr still need a configured download client before a release can complete the normal Grab workflow. In this setup, a Torrent Blackhole client acts as that placeholder so Arr records the Grab event and its download metadata.

The importer does **not** read the files that Blackhole writes. The transport is API-to-API:

1. Arr records the event in history.
2. The importer reads data.downloadUrl from the history entry.
3. The URL is resolved to a magnet where the source returns a redirect.
4. The importer sends TorrServer the magnet plus title, category and metadata.

The Prowlarr URL can contain its API key. The importer resolves the redirect and passes the resulting magnet to TorrServer; it does not persist the credential-bearing downloadUrl into TorrServer's data.

## API overview

| Role | Endpoint | Purpose |
|---|---|---|
| Radarr history | GET /api/v3/history?page=1&pageSize=1000&sortKey=date&sortDirection=descending | Find Grabbed movie releases |
| Sonarr history | GET /api/v3/history?page=1&pageSize=1000&sortKey=date&sortDirection=descending | Find Grabbed series releases |
| Radarr media details | GET /api/v3/movie/{movieId} | Resolve IMDb/TMDb identity and related metadata |
| Sonarr series details | GET /api/v3/series/{seriesId} | Resolve IMDb/TMDb/TVDb identity and related metadata |
| TorrServer get | POST /torrents with {"action":"get","hash":"..."} | Inspect one new or unresolved infohash; no full library snapshot |
| TorrServer add | POST /torrents with {"action":"add",...} | Add a torrent and preserve metadata |

The Arr API key is sent in the X-Api-Key header. The exact download URL is used only to obtain the magnet/direct torrent link; it is not stored in TorrServer data.

The current stored data subset includes source instance name, media type, target-quality lane, IMDb/TMDb/TVDb IDs where known, Arr media and history IDs, download ID, indexer details, release group and custom-format score. Some other fields present in Arr history, such as the full language and Custom Formats arrays, are not yet written into TorrServer data; see the roadmap.

## Installation and operations

- [Installation and configuration](docs/INSTALLATION.md)
- [Architecture and metadata mapping](docs/ARCHITECTURE.md)
- [Operations, rebuilds and troubleshooting](docs/OPERATIONS.md)
- [Roadmap and validation status](ROADMAP.md)
- [Version history](CHANGELOG.md)

The installed and live-validated runtime remains **torr-arr-importer v1.3.5**. The v1.4.0 candidate is developed separately on `feature/nonblocking-hash-get-v1.4.0` and must pass CI and MINI-Ubuntu validation before deployment. The service uses Python 3.11+ and the standard library; it does not require external Python packages.

## Related project: STRM/NFO library generation

[torr2strm](https://github.com/evgendman/torr2strm) is the related downstream project for turning TorrServer's torrent/file metadata into materialized STRM/NFO trees for media-library clients. That repository is currently private, so the link is not publicly accessible unless its visibility changes. torr-arr-importer transfers a chosen release and its identity metadata; torr2strm is responsible for constructing the library representation.

## Current status

MINI-Ubuntu is still running **torr-arr-importer v1.3.5**. The separate v1.4.0 candidate is under development on `feature/nonblocking-hash-get-v1.4.0`; it has not been installed or live-tested on the host. The v1.3.5 source identity and live validation are documented in the historical validation section of [Roadmap](ROADMAP.md).

Live logs on 2026-10-29 confirmed 20 fresh/replacement additions reaching `ADDED verification=usable-file-stats`. For Radarr, seven terminally failed release choices reached the native failed-history action and logged `RADARR_BLOCKLIST_BLOCKED`; three grouped critical Apprise notifications were acknowledged with HTTP 200, and five replacements were verified. For Sonarr, five failed release choices logged `SONARR_BLOCKLIST_BLOCKED`; replacement additions for Outer Banks S01, Outer Banks S03 and Dark S03 were verified. No alternative additions for Love, Death & Robots S02/S03 were observed in the reviewed log window, but the native blocklist actions themselves are confirmed. The blocklist implementation/validation milestone is complete for both applications; replacement availability remains dependent on each app's own settings and available releases.

Remaining v1.3.5 follow-ups are to confirm downstream `torr2strm` processing and reassess the 32 historical success rows created under v1.3.3 without resetting SQLite or resurrecting intentionally removed hashes. The v1.4.0 candidate's CI and live-host validation are tracked separately in [Roadmap](ROADMAP.md).