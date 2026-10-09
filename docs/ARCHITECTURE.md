# Architecture and state model

This document describes the behavior implemented by **torr-arr-importer v1.0.3**. It separates current behavior from work that may be added later.

## 1. Source of truth

The importer reads two live lists on every cycle:

1. the available history in one configured Radarr or Sonarr instance;
2. the current torrent list from TorrServer.

It does not depend on a webhook, an event cursor or a folder watcher. There is no “last processed history date” used as the sole checkpoint. If systemd stops the importer and starts it later, the next cycle scans the available Arr history and reconstructs the candidate set from the current snapshots.

The practical retention boundary is Arr history itself: a Grab that is no longer present in the source history cannot be rediscovered from that source by this importer alone.

## 2. Reading Arr history

The importer calls:

    GET /api/v3/history?page=1&pageSize=1000&sortKey=date&sortDirection=descending
    X-Api-Key: <ARR_API_KEY>
    Accept: application/json

It continues through additional pages while the response's totalRecords indicates that more records remain. history_page_size is limited to 1–1000.

For each record, the importer:

1. keeps only eventType == "grabbed" (case-insensitive);
2. requires a data object;
3. requires data.torrentInfoHash;
4. requires data.downloadUrl;
5. reads the release title, Arr media ID, history ID, download ID, quality object, languages, Custom Formats and Custom Format score into the in-memory Grab record;
6. normalizes the hash to lowercase and deduplicates the history by hash, keeping the newest record.

A grabbed history record that is not a torrent, has no hash, lacks a download URL or lacks a valid data object is skipped and accounted for in the summary log.

The importer does not search Prowlarr/JacRed for the release again. The selected release is already represented by the history record.

## 3. Resolve the release URL

In the tested configuration, the Arr field data.downloadUrl often looks like a local Prowlarr download endpoint:

    http://127.0.0.1:9696/<indexer-id>/download?apikey=...&link=...

The download endpoint may return an HTTP redirect to:

    magnet:?xt=urn:btih:<infohash>&dn=...

The importer follows up to five HTTP redirect hops without treating the credential-bearing URL as the final TorrServer link. Redirect status codes 301, 302, 303, 307 and 308 are supported. If the URL is already a magnet, it is used directly. If a direct HTTP(S) endpoint does not redirect, it is left for TorrServer to consume.

The resolved magnet is not saved into the custom metadata payload. In particular, the Prowlarr API key embedded in the original URL is not copied into TorrServer's data.

## 4. Reading TorrServer

The importer obtains the current server snapshot using:

    POST /torrents
    Content-Type: application/json

    {"action":"list"}

It extracts the hash field from the returned items. Hashes are compared case-insensitively after normalization.

The source and receiver snapshots are independent: Arr history tells us what was grabbed; the TorrServer list tells us what exists now. The importer does not assume that a former successful add is still present.

## 5. Candidate formula

Let:

- **A** = unique infohashes with usable Grabbed records in the configured Arr history;
- **T** = infohashes currently returned by TorrServer;
- **I** = infohashes in the shared SQLite import-history table.

Normal mode:

    Candidates = A − T − I

A hash is not a candidate if it is already in TorrServer or if this importer family has successfully imported it before.

Rebuild mode:

    Candidates = A − T

The --rebuild option ignores the historical exclusion **I**, but still checks current TorrServer membership. It does not truncate or recreate the SQLite database.

This distinction is central to the user-removal policy. If a previously imported torrent is deliberately removed from TorrServer, normal mode sees it in **I** but not **T** and will skip it instead of silently restoring it. A manual rebuild expresses a new decision to repopulate any missing Grabbed items still present in Arr history.

## 6. Shared import history and multiple Arr instances

Every systemd instance uses its own configuration and default lock file, but the configuration examples share:

    /var/lib/torr-arr-importer/state.db

The database records a successfully imported hash after TorrServer has confirmed the expected hash in its response. The same record includes diagnostic fields such as source instance, Arr history/media IDs, title and available provider IDs.

The shared database is not an independent inventory of TorrServer. TorrServer membership is always checked separately. Its purpose is to remember intentional prior imports even after a user removes a torrent from TorrServer.

Example with Radarr and Sonarr both seeing the same hash:

1. Radarr-1080 imports it.
2. The hash is now in TorrServer and in SQLite.
3. Sonarr-1080 scans its history; the hash is already in the TorrServer snapshot and is skipped.
4. Other instances behave the same way.
5. If a user later removes the hash from TorrServer, ordinary runs continue to skip it because it remains in shared SQLite history.
6. If the user intentionally wants to repopulate a clean/new TorrServer, run --rebuild for the relevant Arr instances.

A new hash for another release is a different candidate. Therefore separate releases of the same movie or series can all be added when the user has grabbed them separately.

## 7. Fetch identity metadata from Arr

History records can have empty or zero provider IDs. For a candidate, the importer uses the media ID from the history record to request a detail object.

Radarr:

    GET /api/v3/movie/{movieId}
    X-Api-Key: <RADARR_API_KEY>

Sonarr:

    GET /api/v3/series/{seriesId}
    X-Api-Key: <SONARR_API_KEY>

Within one run, responses are cached by Arr media ID to avoid repeated detail calls for multiple release hashes from the same movie or series.

## 8. TorrServer add request and stored data

The importer adds a candidate through:

    POST /torrents
    Content-Type: application/json

Conceptual payload:

    {
      "action": "add",
      "link": "magnet:?xt=urn:btih:<infohash>",
      "title": "<Arr sourceTitle>",
      "category": "movie",
      "data": "{\"source\":\"radarr-1080\",\"type\":\"movie\", ...}",
      "save_to_db": true
    }

For a Radarr instance, the category is movie; for Sonarr, tv. The importer validates that the configured Arr type and category agree.

The v1.0.3 data payload contains this subset when the value is available:

| Key | Meaning |
|---|---|
| source | Name of the importer instance, e.g. radarr-1080 |
| type | movie or tv |
| target_quality | Configured lane: 1080 or 2160 |
| imdbid, tmdbid, tvdbid | Provider IDs from Arr's movie/series detail response |
| arr_media_id | Arr movie or series record ID |
| arr_history_id | History event ID representing the newest Grabbed record for that hash |
| download_id | Arr download ID, if present |
| indexer | Indexer name from history, if present |
| indexer_id | Indexer ID from history, if present |
| release_group | Release group from history, if present |
| arr_custom_format_score | Custom Format score from history, if present |

The entire history data object is not persisted. In particular, the Prowlarr downloadUrl and nzbInfoUrl are excluded to avoid leaking URL credentials. The history quality, languages and full customFormats arrays are currently read by the code but are not written into TorrServer data in v1.0.3; fuller metadata preservation is a roadmap item.

target_quality is descriptive configuration data. It represents which Arr lane chose the release; it does not measure stream dimensions and must not be treated as a substitute for video metadata.

## 9. Duplicate, retry and failure behavior

- One newest Arr history row is retained per hash before candidate selection.
- TorrServer presence is checked before each add. The in-memory TorrServer hash set is updated after a successful add, so another occurrence of the same hash in the same run is skipped.
- Permanent HTTP 4xx responses, including 401/403, are not retried.
- HTTP 408, 429, 5xx and network failures can be retried according to retry_count and retry_delay.
- API-like URL query credentials are redacted in logs/exceptions.
- Each instance has its own process lock, so overlapping runs of that instance do not run concurrently.
- Failed candidate additions are logged with the hash and a sanitized error; successful additions are recorded in SQLite only after TorrServer confirms the expected hash.
- --dry-run prints candidate decisions and media identifiers but does not add torrents or record successful imports.

## 10. Component boundaries

    Seerr -> Radarr/Sonarr -> Prowlarr / indexer
                     |
                     | Grabbed history + downloadUrl
                     v
              torr-arr-importer
                     |
                     | TorrServer API: add + category + data
                     v
                  TorrServer
                     |
                     | FileStats / play URLs / ffprobe metadata
                     v
                 torr2strm
                     |
                     v
             STRM/NFO media trees

The importer is responsible for moving a selected release and its identity data. A downstream STRM/NFO builder is responsible for turning TorrServer's inventory into media-library paths and files.
