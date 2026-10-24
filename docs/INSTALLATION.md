# Installation and configuration

The instructions below describe the v1.3.1 systemd deployment and its optional Seerr Watchlist integration. The four-instance examples are intended to be adapted to the actual addresses, ports and API keys in your installation.

## Requirements

- Linux with systemd;
- Python **3.11 or newer** (tomllib is part of the Python standard library from 3.11);
- reachable Radarr/Sonarr API endpoints and API keys;
- a reachable TorrServer API endpoint;
- a configured Arr download-client entry that allows the Grab workflow to complete. In this streaming-oriented setup it can be Torrent Blackhole.

No extra pip packages are required.

## 1. Install the program and systemd units

From a local checkout of this repository:

    git clone https://github.com/evgendman/arr2torr.git
    cd arr2torr
    sudo ./install.sh

The installer creates the system account `torrimport` if needed, installs the Python program under `/opt/torr-arr-importer/`, installs the systemd template service and timer, creates state/log directories and runs `systemctl daemon-reload`.

The installer does not enable timers automatically. Configure and test an instance first.

> API keys belong only in local files under /etc/torr-arr-importer/. Never commit a populated config to Git.


## Upgrade an existing v1.1.0 deployment in place

The v1.2.0 source/test revision validated in GitHub Actions is commit `74ee8ac9a8a171be790b5cc314b888cf2e5e19f5` (Python 3.11 and 3.12, 19 tests passed). The upgrade is additive: it adds SQLite columns/tables for notification state, but does not clear or rebuild import history. **Do not delete `/var/lib/torr-arr-importer/state.db`.** The commands below preserve the existing configs and timer enablement.

1. Clone that exact tested revision to a fresh temporary directory, then run tests locally:

       set -euo pipefail
       SRC="$(mktemp -d /tmp/arr2torr-v1.2.0.XXXXXX)"
       git clone https://github.com/evgendman/arr2torr.git "$SRC"
       git -C "$SRC" checkout 74ee8ac9a8a171be790b5cc314b888cf2e5e19f5
       python3 -m py_compile "$SRC/src/importer.py"
       (cd "$SRC" && python3 -m unittest discover -s tests -v)
       python3 "$SRC/src/importer.py" --version
       printf '%s\n' "$SRC" > /tmp/arr2torr-v1.2.0-source-path

2. Stop only the two timers observed active on MINI-Ubuntu, and wait for any oneshot services to finish. Do not enable either 4K timer:

       sudo systemctl stop torr-arr-importer@radarr-1080.timer torr-arr-importer@sonarr-1080.timer
       while systemctl is-active --quiet torr-arr-importer@radarr-1080.service || systemctl is-active --quiet torr-arr-importer@sonarr-1080.service; do sleep 1; done

3. Back up the live source, config, systemd units and database before installation:

       set -euo pipefail
       BACKUP="$(mktemp -d /tmp/arr2torr-v1.1.0-backup.XXXXXX)"
       sudo install -d -m 0700 "$BACKUP"
       sudo cp -a /opt/torr-arr-importer/importer.py "$BACKUP/importer.py"
       sudo cp -a /etc/torr-arr-importer "$BACKUP/config"
       sudo cp -a /etc/systemd/system/torr-arr-importer@.service "$BACKUP/"
       sudo cp -a /etc/systemd/system/torr-arr-importer@.timer "$BACKUP/"
       sudo python3 - "$BACKUP/state.db" <<'PY'
       import os, sqlite3, sys
       source = "/var/lib/torr-arr-importer/state.db"
       if not os.path.isfile(source):
           raise SystemExit(f"State database not found: {source}")
       src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
       dst = sqlite3.connect(sys.argv[1])
       src.backup(dst)
       dst.close()
       src.close()
       PY
       printf '%s\n' "$BACKUP" > /tmp/arr2torr-v1.2.0-backup-path
       echo "Backup saved under: $BACKUP"

4. Add the opt-in Apprise settings described below to **only** `radarr-1080.toml` and `sonarr-1080.toml`. Leave existing API keys and all retry settings unchanged. Set `arr_ui_url` to the matching browser-facing base URL if you want best-effort item-card links; use an empty string to omit links.

5. Install the staged files, without running any import yet:

       SRC="$(cat /tmp/arr2torr-v1.2.0-source-path)"
       sudo "$SRC/install.sh"
       /opt/torr-arr-importer/importer.py --version
       sudo cmp "$SRC/src/importer.py" /opt/torr-arr-importer/importer.py

6. Run dry-runs with the two existing profiles to validate TOML and API reachability. These commands do not add torrents:

       sudo -u torrimport /opt/torr-arr-importer/importer.py --config /etc/torr-arr-importer/radarr-1080.toml --dry-run
       sudo -u torrimport /opt/torr-arr-importer/importer.py --config /etc/torr-arr-importer/sonarr-1080.toml --dry-run

7. Start the same timers again and check the state. This starts the previously enabled 1080p timers; it does not enable new units:

       sudo systemctl start torr-arr-importer@radarr-1080.timer torr-arr-importer@sonarr-1080.timer
       systemctl list-timers 'torr-arr-importer@*' --all --no-pager

If rollback is required, stop those timers and restore the backed-up `importer.py` and systemd unit files from the directory recorded in `/tmp/arr2torr-v1.2.0-backup-path`, then run `sudo systemctl daemon-reload` and start only the same two timers. Do **not** overwrite the live database with the old backup as a routine rollback step: the v1.2.0 schema additions are backward-compatible with v1.1.0, and restoring an old DB could discard importer state created since the backup.

## 2. Instance naming and profiles

Use one config file and one systemd instance per Arr instance:

| Systemd instance | Arr type | TorrServer category | Target-quality metadata |
|---|---|---|---:|
| radarr-1080 | radarr | movie | 1080 |
| radarr-4k | radarr | movie | 2160 |
| sonarr-1080 | sonarr | tv | 1080 |
| sonarr-4k | sonarr | tv | 2160 |

The corresponding configuration paths are:

    /etc/torr-arr-importer/radarr-1080.toml
    /etc/torr-arr-importer/radarr-4k.toml
    /etc/torr-arr-importer/sonarr-1080.toml
    /etc/torr-arr-importer/sonarr-4k.toml

Copy an example and set the real values. For example:

    sudo install -m 0640 -o root -g torrimport \
      config/radarr-1080.toml.example \
      /etc/torr-arr-importer/radarr-1080.toml
    sudoedit /etc/torr-arr-importer/radarr-1080.toml

For other instances, use the matching config/*.toml.example file.

### Required configuration fields

    name = "radarr-1080"
    arr_type = "radarr"
    arr_url = "http://192.168.0.14:7878"
    arr_api_key = "PUT_RADARR_API_KEY_HERE"

    torrserver_url = "http://127.0.0.1:8092"
    category = "movie"
    target_quality = 1080

    state_db = "/var/lib/torr-arr-importer/state.db"
    history_page_size = 1000
    timeout = 30
    log_level = "INFO"
    log_file = "/var/log/torr-arr-importer/radarr-1080.log"
    lock_file = ""
    retry_count = 2
    retry_delay = 1.0
    resolve_download_url = true

- arr_url is the base URL of this exact Radarr/Sonarr instance, without /api/v3.
- arr_api_key is that Arr instance's API key, not the Prowlarr API key.
- torrserver_url points to the receiving TorrServer.
- category must be movie for Radarr and tv for Sonarr. The program validates this relationship.
- target_quality is 1080 or 2160. It is recorded as lane metadata; it does not detect the video's physical resolution.
- **Keep state_db identical in every instance config** if you want global duplicate suppression and the “do not resurrect a deliberately removed torrent” behavior.
- Leave lock_file empty to derive one lock file per instance name. If set explicitly, use a distinct path for each instance.
- An empty log_file sends logs to the system journal only.

For the second Radarr/Sonarr profile, replace the base URL, API key, name, category and target-quality value with those belonging to that instance.

## Optional Apprise notifications (v1.2.0)

Notifications are disabled by default and use the shared Apprise API, not a direct Telegram connection. Add the following flat TOML keys to each instance that should notify. Replace placeholders with values reachable from the Telegram client; do not add a bot token, chat ID or topic ID here.

    notifications_enabled = true
    apprise_url = "http://<MINI-LAN-IP>:8000"
    apprise_config_key = "arr-stack"
    service_name = "arr2torr / Radarr 1080p"
    notification_timeout_sec = 5
    notification_tag_critical = "critical"
    notification_tag_warning = "warnings"
    arr_ui_url = "http://<MINI-LAN-IP>:7878"

Set `service_name` separately in each config, e.g. `arr2torr / Sonarr 1080p`; set `arr_ui_url` to the matching browser-facing Radarr or Sonarr base URL. Leave `arr_ui_url = ""` if you do not want links or the UI base URL is not reachable from Telegram. A link is included only when Arr returns a `titleSlug` and the UI base URL is set.

The current deployed setup only has the 1080p Radarr and Sonarr timers active; keep 4K instance notifications disabled unless those instances are deliberately brought into service.

Behavior:
- A warning goes to `warnings` and contains only counts and add-attempt distribution for items awaiting automatic retry; it contains no names, hashes or item identifiers.
- A final message goes to `critical` when an add/verification attempt becomes terminal or a permanent HTTP 4xx failure is encountered before the TorrServer add. Newly terminal items are grouped into as few messages as practical; each item includes its media title, release title when available, a sanitized cause, attempt information and an optional Arr card link.
- The fingerprint for retry warnings and the one-time terminal-notification marker live in the shared SQLite database. Existing `failed` rows present before the v1.2.0 migration are baselined so an upgrade does not dump old failures into Telegram.
- An Apprise outage is logged locally only; the importer does not fail solely because notification delivery failed and does not retry the same terminal alert indefinitely.


## Optional Seerr Watchlist integration (v1.3.0)

This feature is disabled by default. Add these flat TOML keys to the existing Radarr/Sonarr profile that should register terminal failures in Seerr:

    seerr_watchlist_enabled = false
    seerr_url = "http://192.168.0.14:5055"
    seerr_api_key = "PUT_SEERR_API_KEY_HERE"

Replace the placeholder key locally; never commit an actual key or put it in logs. The Watchlist is personal, so the API-key request targets the administrator account (Seerr user ID 1). Do not add `X-API-User` to override it.

For the current MINI-Ubuntu setup, only edit:
- `/etc/torr-arr-importer/radarr-1080.toml`
- `/etc/torr-arr-importer/sonarr-1080.toml`

Both files should keep `seerr_watchlist_enabled = false` until the real API key has been saved and the feature is ready for activation. Enable by changing the value to `true` in those two files only. Keep 4K profiles disabled unless explicitly required.

When enabled, the importer selects only terminal `failed` rows for the current Arr instance and adds their existing TMDb IDs to `POST /api/v1/watchlist`. Series are registered as a whole title. Missing IDs are reported rather than looked up externally. The feature does not create a Seerr media request, intentionally alter Arr monitoring, or initiate a second Arr search.

Notifications reuse the existing Apprise config:
- After a failure is represented in Watchlist (including a duplicate whose presence is verified), send type `warning` to tag `warnings`. Explain that TorrServer transfer failed and the item is listed in the administrator's Seerr Watchlist for manual investigation; this is not a media-available notice.
- If registration itself fails, send type `failure` to tag `critical`.
- Watchlist state and notification fingerprints are stored in the existing SQLite database. API/network errors can retry on later timer runs; unchanged error alerts are deduplicated.

### Safe activation sequence

1. Stop only the two currently used timers before changing the live configuration:

       sudo systemctl stop torr-arr-importer@radarr-1080.timer torr-arr-importer@sonarr-1080.timer
       while systemctl is-active --quiet torr-arr-importer@radarr-1080.service || systemctl is-active --quiet torr-arr-importer@sonarr-1080.service; do sleep 1; done

2. Edit the two files and replace the placeholder API key. Set `seerr_watchlist_enabled = true` in each file only after entering the real key:

       sudo nano /etc/torr-arr-importer/radarr-1080.toml
       sudo nano /etc/torr-arr-importer/sonarr-1080.toml

3. Validate config parsing without triggering a transfer by running `--dry-run`. Since the Seerr feature is disabled from all dry-run side effects, no Watchlist entries or notifications are created:

       sudo -u torrimport /opt/torr-arr-importer/importer.py --config /etc/torr-arr-importer/radarr-1080.toml --dry-run
       sudo -u torrimport /opt/torr-arr-importer/importer.py --config /etc/torr-arr-importer/sonarr-1080.toml --dry-run

4. Before restarting timers, manually validate the Seerr API with one selected test item (a disposable movie is preferred): confirm `POST /api/v1/watchlist` returns 201 and the item appears in the administrator profile. Repeat the POST to observe the duplicate response, then verify `GET /api/v1/user/1/watchlist?page=1` distinguishes a local Seerr Watchlist entry. Remove the disposable item in Seerr when done. Repeat with a test series if needed. Do not use a genuine failed production item as a test unless explicitly selected.

5. Once the real key and the API behavior are verified, start the same two timers again. Do not enable the 4K timers:

       sudo systemctl start torr-arr-importer@radarr-1080.timer torr-arr-importer@sonarr-1080.timer
       systemctl list-timers 'torr-arr-importer@*' --all --no-pager

## 3. Dry-run before enabling the timer

Run as the service account:

    sudo -u torrimport /opt/torr-arr-importer/importer.py \
      --config /etc/torr-arr-importer/radarr-1080.toml \
      --dry-run

Look at the HISTORY and SNAPSHOT lines. They show how many records were read, how many unique torrent hashes were found, how many hashes already exist in TorrServer, how many were previously imported and how many candidates remain.

A dry-run may call Arr's movie/series endpoint to retrieve IDs for candidates. It does not add torrents to TorrServer and does not mark candidates as successfully imported.

## 4. First real run

Once the candidate list is correct, perform one manual import:

    sudo -u torrimport /opt/torr-arr-importer/importer.py \
      --config /etc/torr-arr-importer/radarr-1080.toml

Then inspect the log and TorrServer's list:

    sudo journalctl -u torr-arr-importer@radarr-1080.service -n 100 --no-pager
    curl -fsS -H 'Content-Type: application/json' \
      -d '{"action":"list"}' http://127.0.0.1:8092/torrents

The command-line manual run uses the local configuration. The systemd service uses the same file, so validating a one-off run first is recommended.

## 5. Enable periodic synchronization

After manual run behavior is correct:

    sudo systemctl enable --now torr-arr-importer@radarr-1080.timer

Repeat for each configured instance:

    sudo systemctl enable --now torr-arr-importer@radarr-4k.timer
    sudo systemctl enable --now torr-arr-importer@sonarr-1080.timer
    sudo systemctl enable --now torr-arr-importer@sonarr-4k.timer

The default timer template runs once about 30 seconds after boot and then every five minutes:

    OnBootSec=30s
    OnUnitActiveSec=5min
    AccuracySec=10s
    Persistent=true

Each timer is an independent systemd instance, even though the schedule defaults are shared. To avoid all four instances doing their API reads at the same moment, enable them at different times or set per-instance timer drop-ins.

Example: give sonarr-4k a different start offset while keeping the five-minute period:

    sudo systemctl edit torr-arr-importer@sonarr-4k.timer

Use this override:

    [Timer]
    OnBootSec=
    OnUnitActiveSec=
    OnBootSec=2min
    OnUnitActiveSec=5min
    AccuracySec=10s
    Persistent=true

After editing a timer drop-in:

    sudo systemctl daemon-reload
    sudo systemctl restart torr-arr-importer@sonarr-4k.timer

Choose different offsets for other instances if needed. The Python process lock prevents overlapping runs of the same instance; staggered timers reduce simultaneous calls across different instances.

## 6. Check timer and service status

    systemctl list-timers 'torr-arr-importer@*' --no-pager
    systemctl status torr-arr-importer@radarr-1080.timer --no-pager
    journalctl -u torr-arr-importer@radarr-1080.service -n 100 --no-pager

Useful service names:

    torr-arr-importer@radarr-1080.service
    torr-arr-importer@radarr-1080.timer
    torr-arr-importer@radarr-4k.service
    torr-arr-importer@radarr-4k.timer
    torr-arr-importer@sonarr-1080.service
    torr-arr-importer@sonarr-1080.timer
    torr-arr-importer@sonarr-4k.service
    torr-arr-importer@sonarr-4k.timer

Each timer triggers a oneshot service. The importer exits after one cycle; systemd schedules the next one.

## 7. Security and permissions

The intended local file layout is:

    /opt/torr-arr-importer/importer.py
    /etc/torr-arr-importer/*.toml
    /var/lib/torr-arr-importer/state.db
    /var/log/torr-arr-importer/*.log
    /etc/systemd/system/torr-arr-importer@.service
    /etc/systemd/system/torr-arr-importer@.timer

The service runs as torrimport, with restricted systemd settings and write access to its state/log directories. Config files should be readable by root and group torrimport, mode 0640, and must not be committed with real API keys.
