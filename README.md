# arr2torr

**Move selected Radarr and Sonarr releases to TorrServer for streaming — without waiting for a full download.**

arr2torr is the project and documentation home for **torr-arr-importer**, a polling service that transfers releases selected with **Grab** in Radarr or Sonarr directly to the TorrServer API. It is intended for a media workflow where the user wants to start streaming from TorrServer, then expose those streams to a media library through STRM files or another integration.

The current implementation described here is **torr-arr-importer v1.3.1**.

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
- Treats the TorrServer add response as an acknowledgement, then checks observable readiness or explicit database-presence evidence before recording success.
- Submits candidates first and verifies the batch with one polling loop and per-hash deadlines rather than serially waiting for each add.
- Retries unverified attempts with bounded exponential backoff and avoids resending hashes that are still pending on TorrServer.
- Supports non-mutating --dry-run, deliberate --rebuild, and explicit --retry-failed for exhausted unverified attempts.
- Uses separate per-instance configuration and lock files, while sharing the import-history database.
- Redacts API-key-like URL parameters from logs and exceptions.

## Asynchronous add verification (v1.1.0)

TorrServer may acknowledge an add request before its background metadata acquisition completes. An expected hash in the initial response is therefore not treated as final success.

The importer persists a pending attempt before the add request, submits eligible releases first, then polls the TorrServer list at a configurable interval. Every pending hash has its own deadline. Usable FileStats, a working status, or TorrServer's explicit database-presence status is used as observable verification evidence. The successful-import table is updated only after this evidence is observed.

Pending attempts live in the shared SQLite table pending_adds and do not contain the credential-bearing download URL. After a process restart, they are reconciled against TorrServer before retrying. Failures use bounded exponential backoff and stop automatically after the configured attempt limit; --retry-failed explicitly re-queues exhausted, never-confirmed attempts for the selected Arr instance.

Verification means observable readiness or database presence; it does not claim a durable disk-commit guarantee that TorrServer's API does not expose. Older successful-import records remain protected from automatic resurrection. Use --rebuild only when intentionally repopulating missing historical entries.

Default verification settings are verification_timeout_sec = 120, verification_poll_interval_sec = 3.0, retry_backoff_sec = 60 (doubling per failed attempt, capped at one hour), and max_add_attempts = 3. Compare the timeout with the TorrentDisconnectTimeout setting of the installed TorrServer build.

## Final-failure Apprise notifications (next revision)

Notifications are opt-in and use the shared Apprise API; arr2torr never stores Telegram tokens, chat IDs or topic IDs. **There are no Telegram messages while an item is still waiting for an automatic add/verification retry.** The importer waits until the failure is terminal (all configured add attempts are exhausted, or a permanent error makes retry impossible).

The final message goes to the critical tag and brings the entire action-oriented summary together: movie/series title, selected release, plain-language reason, attempts used, recommendation to choose another release in Radarr/Sonarr, and a direct Arr card link when available. When Seerr Watchlist integration is enabled, the same message reports whether the media was added, was already listed, or could not be registered. There is no second warning or Watchlist message for the same failure.

Terminal notification state is stored in the existing SQLite database and is migrated in place. An unsuccessful Apprise request leaves the message pending and schedules another attempt with increasing delays (1, 5, 15, 30 and then 60 minutes, capped at one hour). A successful 2xx response marks the notification delivered. This is at-least-once delivery: if Apprise accepts a request but its response is lost, a retry can result in a duplicate. Delivery failures never change the TorrServer import result. Notifications remain disabled in example configs by default. See [Installation and configuration](docs/INSTALLATION.md) and [Operations](docs/OPERATIONS.md).

## Seerr Watchlist for terminal transfer failures (v1.3.0)

The optional Seerr integration registers a movie or whole series in the administrator's personal Seerr Watchlist after a TorrServer transfer reaches terminal failure. It is an attention marker for manual review, **not** a Seerr media request: it does not ask Seerr to search/download the title and does not deliberately change Radarr/Sonarr monitoring or trigger their search commands.

- Controlled by `seerr_watchlist_enabled`, which defaults to `false`.
- Requires `seerr_url` and `seerr_api_key`. API-key requests use Seerr's administrator account (user ID 1) unless an explicit user override is sent; this integration does not send one.
- Reads terminal `failed` rows from `pending_adds` and permanent `terminal_events` for the current Arr instance. `pending` and `retry_wait` are ignored.
- Uses only the TMDb ID already stored in arr2torr. Missing IDs are recorded and reported; there is no separate metadata lookup.
- The Seerr registration result is included in the same final critical transfer-failure message; there is no separate warnings or Seerr notification. A successful or verified-existing Watchlist entry is described naturally, while registration errors are reported honestly and are retried by the integration where applicable.
- Watchlist processing state is stored in additional SQLite columns and is migrated in place. The import history and original TorrServer failure state remain intact.
- The success notification is not an availability notification: a Watchlist item is **not necessarily available to watch**.

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
| TorrServer list | POST /torrents with {"action":"list"} | Snapshot existing infohashes |
| TorrServer add | POST /torrents with {"action":"add",...} | Add a torrent and preserve metadata |

The Arr API key is sent in the X-Api-Key header. The exact download URL is used only to obtain the magnet/direct torrent link; it is not stored in TorrServer data.

The current stored data subset includes source instance name, media type, target-quality lane, IMDb/TMDb/TVDb IDs where known, Arr media and history IDs, download ID, indexer details, release group and custom-format score. Some other fields present in Arr history, such as the full language and Custom Formats arrays, are not yet written into TorrServer data; see the roadmap.

## Installation and operations

- [Installation and configuration](docs/INSTALLATION.md)
- [Architecture and metadata mapping](docs/ARCHITECTURE.md)
- [Operations, rebuilds and troubleshooting](docs/OPERATIONS.md)
- [Roadmap and validation status](ROADMAP.md)
- [Version history](CHANGELOG.md)

The current code package is **torr-arr-importer v1.3.1**. It uses Python 3.11+ and the standard library; it does not require external Python packages.

## Related project: STRM/NFO library generation

[torr2strm](https://github.com/evgendman/torr2strm) is the related downstream project for turning TorrServer's torrent/file metadata into materialized STRM/NFO trees for media-library clients. That repository is currently private, so the link is not publicly accessible unless its visibility changes. torr-arr-importer transfers a chosen release and its identity metadata; torr2strm is responsible for constructing the library representation.

## Current status

The installed MINI-Ubuntu implementation is torr-arr-importer **v1.3.1** (deployed 2026-10-24). The Radarr and Sonarr 1080p timer instances are the production instances; this does not imply that either 4K timer is enabled.

Historical v1.2.0 validation on 2026-10-22 confirmed end-to-end Apprise/Telegram sends for both warning and critical messages. The proposed notification redesign supersedes that behavior: it suppresses all user-facing messages during automatic retries and sends only one grouped critical summary when a failure becomes terminal. That summary includes the Arr card link and Seerr Watchlist status when available. This redesign has not yet been installed on MINI-Ubuntu.

The redesign is also changing delivery semantics. Terminal events remain pending in the shared SQLite database until Apprise returns a successful 2xx response. Delivery errors are retried with persistent exponential backoff; this avoids silently discarding an alert after a single network/HTTP failure. Because an HTTP timeout can occur after the receiving API accepted a request, delivery is at-least-once rather than a strict exactly-once guarantee.

The importer remains conservative: TorrServer membership and shared SQLite history are compared on each run, only verified additions enter successful import history, and normal operation does not override a deliberate removal decision. Successful transfer verification still does not guarantee that peers/network conditions will make playback smooth.
