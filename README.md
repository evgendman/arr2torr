# arr2torr

**Move selected Radarr and Sonarr releases to TorrServer for streaming — without waiting for a full download.**

arr2torr is the project and documentation home for **torr-arr-importer**, a polling service that transfers releases selected with **Grab** in Radarr or Sonarr directly to the TorrServer API. It is intended for a media workflow where the user wants to start streaming from TorrServer, then expose those streams to a media library through STRM files or another integration.

The current implementation described here is **torr-arr-importer v1.0.3**.

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
- Uses one shared SQLite database across all importer instances to remember hashes that were successfully imported.
- Supports a non-mutating --dry-run and a deliberate --rebuild mode.
- Uses separate per-instance configuration and lock files, while sharing the import-history database.
- Redacts API-key-like URL parameters from logs and exceptions.

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

That shared history makes the instances cooperate instead of duplicating work. If the same infohash appears in different Arr histories, whichever instance sees it first can add it; the others see the hash in TorrServer and skip it.

The configured target_quality is stored as descriptive metadata from the source instance. It helps preserve which Arr lane selected the release, but it is **not** proof of the actual video resolution. A downstream STRM builder should inspect real stream metadata if the physical quality bucket must be accurate.

## How candidates are selected

For a normal run, define:

- **A** — distinct hashes in the source Arr's Grabbed history;
- **T** — hashes currently present in TorrServer;
- **I** — hashes in the shared persistent import history.

The normal candidate set is:

    Candidates = A − T − I

The importer adds only hashes that have not already been imported, are not currently in TorrServer, and have not previously been recorded as imported.

For an intentional resynchronization, --rebuild ignores **I**:

    Rebuild candidates = A − T

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

The current code package is **torr-arr-importer v1.0.3**. It uses Python 3.11+ and the standard library; it does not require external Python packages.

## Related project: STRM/NFO library generation

[torr2strm](https://github.com/evgendman/torr2strm) is the related downstream project for turning TorrServer's torrent/file metadata into materialized STRM/NFO trees for media-library clients. That repository is currently private, so the link is not publicly accessible unless its visibility changes. torr-arr-importer transfers a chosen release and its identity metadata; torr2strm is responsible for constructing the library representation.

## Current status

The v1.0.3 implementation has been exercised against the live Radarr and TorrServer APIs. The Radarr path has passed real add, repeat-run deduplication, “do not resurrect a removed torrent” and rebuild checks. Sonarr history parsing and duplicate-hash behavior have been checked; an end-to-end live test where a **new** Sonarr Grab is imported remains on the validation list.

The importer is designed to be conservative: TorrServer membership and the shared history are compared on every run, failed additions are logged, and normal operation does not override a deliberate removal decision.
