# Architecture and state model

This document describes the **v1.4.1 candidate** on branch `feature/in-run-get-verification-v1.4.1`. GitHub Actions run [#156](https://github.com/evgendman/arr2torr/actions/runs/41210258165) passed on Python 3.11, 3.12 and 3.14 with 61 tests per version, compilation and version check. The candidate is not deployed. MINI-Ubuntu currently has v1.4.0 candidate code on disk but both importer timers are stopped; that code has not been run against production SQLite. The last live-validated behavior was v1.3.5. Comparative CPU/request measurements and live-host validation remain.

## 1. Source of truth

The importer reads the available history in one configured Radarr or Sonarr instance on each timer run. It does not depend on a webhook, an event cursor or a folder watcher. Arr history remains the discovery source for newly grabbed releases.

The shared SQLite database records two durable facts:

- `imports`: hashes previously confirmed with usable `FileStats`, including hashes deliberately removed by the user after confirmation;
- `pending_adds`: hashes whose existence/readiness is still unresolved, including the number of actual `add` submissions and their retry state.

The importer does not maintain or fetch a full inventory of TorrServer. It asks TorrServer only about specific new or unresolved hashes. If Arr history and SQLite contain no work requiring observation, the run makes no TorrServer API request.

The practical retention boundary remains Arr history itself: a Grab no longer present in the source history cannot be newly rediscovered from that source alone.

## 2. Reading Arr history

The importer calls:

    GET /api/v3/history?page=1&pageSize=1000&sortKey=date&sortDirection=descending
    X-Api-Key: <ARR_API_KEY>
    Accept: application/json

It continues through additional pages while the response's totalRecords indicates that more records remain. history_page_size is limited to 1–1000.

For each record, the importer:

1. keeps only eventType == "grabbed" (case-insensitive);
2. requires a data object;
3. requires data.torrentInfoHash and data.downloadUrl;
4. reads the release title, Arr media ID, history ID, download ID and selected Arr metadata into the in-memory Grab record;
5. normalizes the hash to lowercase and deduplicates history by hash, keeping the newest record.

The importer does not search Prowlarr/JacRed for the release again. The selected release is already represented by the history record.

## 3. Resolve the release URL

In the tested configuration, the Arr field data.downloadUrl often looks like a local Prowlarr download endpoint:

    http://127.0.0.1:9696/<indexer-id>/download?apikey=...&link=...

The download endpoint may return an HTTP redirect to a magnet link. The importer follows up to five HTTP redirect hops without treating the credential-bearing URL as the final TorrServer link. Redirect status codes 301, 302, 303, 307 and 308 are supported. If the URL is already a magnet, it is used directly. If a direct HTTP(S) endpoint does not redirect, it is left for TorrServer to consume.

The resolved link is passed to TorrServer but is not saved into the custom metadata payload. In particular, the Prowlarr API key embedded in the original URL is not copied into TorrServer data or SQLite.

## 4. Hash-addressed TorrServer access

The v1.4.1 candidate does not call `POST /torrents {"action":"list"}` in ordinary processing or rebuild mode. Instead it calls:

    POST /torrents
    Content-Type: application/json

    {"action":"get","hash":"<exact-infohash>"}

This request is made only for a new Arr hash or a hash that remains pending. Before the first get, the importer atomically claims the hash in the shared SQLite database. A competing Arr instance therefore skips the hash instead of issuing a duplicate get or add.

The first pass checks all unresolved pending hashes and eligible new candidates. Any hash that remains incomplete or uncertain enters one shared in-run verification carousel. The carousel sweeps the waiting hashes using exact-hash `action=get` at the configured interval; it does not repeatedly fetch the full TorrServer inventory.

Relevant outcomes:

- **Usable FileStats:** record the hash in shared `imports` and remove it from the queue immediately.
- **Object exists but FileStats are incomplete:** retain a pending row and continue polling; do not submit a duplicate add.
- **HTTP 404 for a new candidate:** prepare media/link data, atomically claim the hash, then submit one `action=add`.
- **HTTP 404 after a previously accepted add:** do not immediately resend. Count at most one missing observation per importer execution; two observations on separate runs are required before the bounded retry/backoff path.

Network errors and HTTP 5xx responses are unknown observations, not evidence that a torrent is absent. They do not consume the add-attempt counter.

TorrServer MatriX.145 `action=get` can invoke its database-backed reload path when a hash exists in the TorrServer database but is not active in memory. Such a response commonly reports `Torrent in db` without usable FileStats. The same run's subsequent carousel reads observe the reload; the importer does not send a duplicate metadata-activation `add`.

## 5. Candidate and pending-state model

Let:

- **A** = unique infohashes with usable Grabbed records in the current Arr history;
- **I** = hashes in the shared SQLite verified-import history;
- **P** = hashes in the shared SQLite pending table.

In normal mode, a hash in **I** is excluded without querying TorrServer. A hash in **P** is handled only through its existing state and owning Arr instance. New candidates are:

    New candidates = A − I − P

For every unresolved hash, the current run performs repeated exact-hash `get` observations in one shared carousel. A candidate enters the queue when its first read is incomplete/uncertain or after an accepted add does not immediately contain FileStats. Its own `verification_timeout_sec` deadline (default 120 seconds) begins at queue entry.

- Usable FileStats promote the row to `imports` and remove it from the queue immediately.
- An incomplete object stays pending and is checked on the next carousel sweep.
- An individual timeout ends only the current run's wait for that hash. The row remains pending for the next timer run; timeout does not increment `attempt_count`, create a metadata-failure counter or trigger terminal blocklisting.
- Two 404 observations from separate importer runs after a previous accepted add move the row into retry/backoff or terminal failure according to `max_add_attempts`.

Only actual `action=add` submissions increment `attempt_count`. Atomic claims and read-only `get` observations do not consume attempts. Automatic HTTP retries are disabled for `action=add`; ambiguous outcomes are reconciled by later hash-addressed reads.

A successful import means a non-empty `FileStats` list for the exact expected hash, with positive unique file IDs and unique safe relative paths. HTTP 200, an echoed hash, database membership or a status string alone is never sufficient.

Rebuild mode ignores **I** for that execution but still uses exact-hash `get` to avoid duplicating a ready object. It does not truncate or recreate SQLite. A rebuild is the explicit operator-controlled exception to the intentional-removal safeguard.

## 6. Shared import history and multiple Arr instances

Every systemd instance uses its own configuration and default lock file, but the configuration examples share:

    /var/lib/torr-arr-importer/state.db

The shared database coordinates Radarr/Sonarr 1080p/4K instances. If two instances see the same new hash, both may discover it in their own Arr histories, but only one can atomically claim the shared pending row and submit \`add\`. Other instances see the shared claim and skip duplicate work.

A hash confirmed in \`imports\` is not queried again in ordinary mode, regardless of which Arr instance first observed it. If a user deliberately removes that torrent from TorrServer, normal timer runs do not resurrect it because the verified hash remains in SQLite. Use \`--rebuild\` only when intentionally repopulating a clean/new TorrServer or otherwise overriding this safeguard.

The v1.3.3 \`imports\` table did not record the verification method. The candidate does not automatically reclassify old success rows or resurrect missing hashes; the 32 historical rows created under that weaker rule still require separate, cautious reassessment.


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

The current TorrServer data payload contains this subset when the value is available:

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

The entire history data object is not persisted. In particular, the Prowlarr downloadUrl and nzbInfoUrl are excluded to avoid leaking URL credentials. The history quality, languages and full customFormats arrays are currently read by the code but are not written into the TorrServer data payload; fuller metadata preservation is a roadmap item.

target_quality is descriptive configuration data. It represents which Arr lane chose the release; it does not measure stream dimensions and must not be treated as a substitute for video metadata.

## 9. Duplicate, retry and failure behavior

- One newest Arr history row is retained per hash before candidate selection.
- Only usable FileStats for the exact hash satisfy the success contract.
- New and pending hashes are inspected using address-specific action=get; ordinary processing does not retrieve the full TorrServer inventory.
- Present-but-incomplete and DB-only objects remain pending. MatriX.145 action=get may initiate a database-backed reload; no separate metadata-activation add is sent.
- A 404 after an earlier add is observed on two separate timer runs before the existing bounded retry/backoff policy is entered.
- Permanent HTTP 4xx responses, including 401/403, are not retried automatically by the HTTP client.
- HTTP 408, 429, 5xx and network failures from read-only requests can be retried according to retry_count and retry_delay. The side-effecting TorrServer action=add uses retry_count=0; uncertain results are reconciled by later exact-hash reads.
- Only actual add submissions increment max_add_attempts. Hash reservations and get observations do not consume attempts.
- API-like URL query credentials are redacted in logs/exceptions.
- Each instance has its own process lock. The shared SQLite pending claim prevents overlapping Arr instances from adding the same hash concurrently.
- Successful new/pending imports are recorded in SQLite only after usable FileStats are observed for the exact hash.
- --dry-run prints candidate decisions and media identifiers but does not call TorrServer or alter import/pending state.

## 10. Notification state and failure summaries

The implemented terminal-only behavior sends no messages for pending or retry_wait attempts. Once a failure is terminal—or a permanent preparation error prevents the TorrServer add—the importer creates a grouped critical/failure message with media title, selected release title, a plain-language cause, attempts used or a permanent-error explanation, a recommendation to choose another release, and an optional Arr card link. If Seerr Watchlist is enabled, the same message includes the latest registration result. No separate warnings message or Seerr notification is emitted.

Terminal notification state is extended in place in the existing SQLite DB:
- notification_attempted_at is set only after a successful 2xx response from Apprise.
- notification_attempt_count, notification_next_retry_at and notification_last_error retain delivery retry state when Apprise is unavailable.
- Retry delays increase (1, 5, 15, 30 and then 60 minutes, one-hour cap) and survive process restarts.
- Permanent errors before an add are deduplicated in terminal_events; failed verification attempts are persisted in pending_adds.
- Distinct terminal rows without TMDb IDs remain distinct messages; the notification path does not merge them by a missing identifier.

The optional arr_ui_url is a browser-facing base URL, separate from arr_url (the API base). A link is built from the Arr titleSlug and the instance type (/movie/<slug> for Radarr or /series/<slug> for Sonarr); if either value is unavailable, the alert omits the link.

Apprise delivery is at-least-once. If a request times out after Apprise has already accepted it, retrying may duplicate the message; strict exactly-once delivery cannot be inferred from a timed-out HTTP request. Delivery failures are logged locally and do not change import/retry state or trigger recursive alerts. The legacy notification_tag_warning setting is still parsed for compatibility, but current user-facing alerts use only notification_tag_critical.

## 11. Component boundaries

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
