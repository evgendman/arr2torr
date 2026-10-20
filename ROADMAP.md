# Roadmap

This document records the current **torr-arr-importer v1.1.0** implementation separately from remaining work and live validation.

## Implemented

- One standard-library Python implementation for both Radarr and Sonarr.
- One systemd service/timer instance per configured Arr instance.
- Polling the paginated Arr /api/v3/history endpoint on each run.
- Filtering to usable Grabbed records and normalizing/deduplicating their BitTorrent infohashes.
- Retrieving the magnet or direct torrent URL through the history data.downloadUrl, including resolution of HTTP redirects.
- Snapshotting TorrServer through POST /torrents with {"action":"list"}.
- Adding through TorrServer POST /torrents with title, movie/tv category, safe custom data JSON and save_to_db: true.
- Fetching Radarr movie or Sonarr series details to obtain trustworthy provider IDs when they are missing from history.
- Shared SQLite success history across all Arr instances, plus a pending-add table for restart-safe asynchronous verification.
- Per-instance lock files, retry handling for transient HTTP/network failures and redaction of API-key-like URL parameters.
- --dry-run and --rebuild.
- Protection against automatic re-adding after a user removes a known imported torrent from TorrServer.
- Useful summary logs (HISTORY, SNAPSHOT, ADD, ADD_ACCEPTED, ADDED, VERIFY_FAILED, FAILED, DONE).
- Batch verification of accepted add requests using per-hash deadlines, shared list polling and readiness/database-presence evidence.
- Atomic pending claims across Arr instances, persistent retry state, bounded exponential backoff and explicit --retry-failed recovery.

## Validated so far

- **Radarr live path:** multiple current Grabbed records imported into TorrServer with category and data.
- **Idempotency:** a second normal run sees the hashes in TorrServer and adds none.
- **Removal memory:** after a known hash is removed from TorrServer, a normal run skips it instead of restoring it.
- **Rebuild:** a missing hash is added again when --rebuild is deliberately used.
- **Incremental update:** a new Radarr Grab was observed in history and automatically imported on a subsequent timer cycle, including provider IDs.
- **Sonarr history path:** records parse and duplicate hashes are recognized correctly; a dry-run with a hash already in TorrServer produced no candidate.

The last item is not the same as a complete live test of a **new** Sonarr Grab flowing into an initially missing TorrServer hash. That end-to-end case remains to be verified.
- **Automation:** GitHub Actions compiles the importer and runs 12 unit tests on Python 3.11 and 3.12; both matrix jobs passed for v1.1.0.

## Planned improvements

### 1. Preserve more useful Arr metadata

v1.0.3 reads the history quality, languages and customFormats fields but the current TorrServer data payload does not persist those complete objects. Consider adding a carefully versioned and sanitized subset, for example:

- quality name/source/resolution from the grabbed decision;
- selected audio languages;
- Custom Format names and scores;
- protocol and release-type metadata where useful;
- other stable fields that help downstream NFO generation or release display.

Only non-secret, stable metadata should be stored. Credential-bearing downloadUrl and similar links must remain excluded.

### 2. Complete end-to-end Sonarr validation

Create a controlled new Sonarr Grab whose hash is absent from TorrServer. Confirm the timer sees it, resolves the download link, imports it under category tv, records the provider IDs, and skips it on the next ordinary cycle.

### 3. Make timer cadence easier to configure per instance

The current template defaults all instances to five minutes. Systemd timer drop-ins can already override offsets/intervals, but the project should provide clear examples or a supported configuration scheme so four instances can be staggered without guesswork.

### 4. Document and test clean-destination recovery

--rebuild already implements the necessary behavior without deleting shared SQLite state. Add repeatable tests that cover replacing the TorrServer database while keeping importer history, then rebuilding each Arr source without duplicate adds.

### 5. Add repeatable tests and recorded API fixtures

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

### 6. Verify asynchronous TorrServer adds before recording successful imports

**Status: core implementation in v1.1.0; Python 3.11/3.12 CI passes. Live integration and additional failure-fixture coverage remain.**

**Reason**

TorrServer's `POST /torrents` `action=add` response is not necessarily proof that the asynchronous metadata-acquisition step completed or that `save_to_db=true` reached its database-save path. The response can contain the expected hash before the server's background `GotInfo()` operation finishes. If metadata acquisition later times out, TorrServer may close the in-memory torrent without saving it, while the current importer has already written the hash to its shared SQLite import history. This can leave a false success marker that suppresses normal retries.

**Work**

- [x] Treat the add response as an acknowledgement that the request was accepted, not as final proof of successful persistence.
- After `action=add`, independently poll TorrServer using its supported list/get APIs for the exact normalized infohash. Require observable readiness evidence (prefer a usable, non-empty file list/FileStats or a trustworthy ready/working status), not merely an immediate echo of the requested hash.
- [x] Send eligible candidates first and track each accepted add as its own pending operation, with its own deadline starting at request submission.
- [x] After submission, use a bounded shared `action=list` polling loop to check pending hashes together; fast-ready torrents are verified while slower torrents continue waiting.
- [x] Keep polling until every pending add reaches a terminal result or its individual deadline expires; individual deadlines prevent an unbounded wait.
- [x] Make verification timeout and polling interval configurable, and document that defaults must be compared with the installed TorrServer timeout.
- [x] Log request acceptance, uncertain responses, verification success/failure and polling errors with the hash and observed status.
- [x] Promote a hash into successful-import history only after verification; store pending attempt metadata and status in the shared SQLite database.
- [x] Reconcile persisted attempts against TorrServer before new adds; observe present-but-unready hashes, and retry absent unverified hashes only after bounded backoff and within the attempt limit.
- [x] Keep pending/uncertain attempts separate from successful history and preserve the intentional-removal safeguard.
- [x] Leave unverified attempts out of successful history, preserve failure details, and require a fresh membership check before retry.
- [x] Preserve the deliberate-removal safeguard for verified imports; historical recovery remains explicit via --rebuild.
- [x] Describe the guarantee as observable readiness/database presence, not an independently proven durable disk commit.
- Add recorded API fixtures and tests for immediate success, delayed readiness, a correct hash returned before later timeout/disappearance, malformed/missing/mismatched hashes, transient API errors, retry after unverified failure, concurrent instances, and intentional user removal. Ensure an unverified attempt cannot suppress a subsequent eligible retry.

**Exit criteria**

- A successful add is not recorded as completed solely because the initial response contains the expected hash.
- Delayed or failed asynchronous adds are visible in logs/state and remain safely recoverable without duplicate-add loops.
- A batch does not exit while it still has pending verification before the configured per-hash deadlines; fast successes are confirmed promptly, and one slow torrent does not force separate serial waits for every other torrent.
- Pending additions survive importer restarts and are reconciled against TorrServer before any retry.
- Ordinary retries remain idempotent; confirmed intentional removals are not resurrected.
- Tests demonstrate both the successful readiness path and the “initial API acknowledgement, later metadata timeout” failure path.

### 7. Coordinate with the downstream STRM/NFO project

The importer already preserves provider IDs and selected Arr fields in TorrServer data, which supports a downstream library tree. The related torr2strm repository is currently private. When its public status and API are settled, document an end-to-end example showing Seerr request → Arr Grab → TorrServer → STRM/NFO library.

## Explicitly not planned

- A webhook listener: polling the Arr history is the selected design.
- A Blackhole directory watcher: Blackhole permits Arr's Grab workflow, but the importer uses the Arr API and TorrServer API.
- Downloading media inside the importer: TorrServer is the receiving/streaming service.
- Silently resurrecting user-removed torrents during normal polling: rebuild must remain an explicit action.
