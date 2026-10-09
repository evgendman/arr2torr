# Installation and configuration

The instructions below describe the v1.0.3 systemd deployment used on the Ubuntu media server. The four-instance examples are intended to be adapted to the actual addresses, ports and API keys in your installation.

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
