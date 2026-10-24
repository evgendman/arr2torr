#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

install -d -m 0755 /opt/torr-arr-importer /etc/torr-arr-importer /var/lib/torr-arr-importer /var/log/torr-arr-importer
install -d -m 0755 /etc/systemd/system

if ! id -u torrimport >/dev/null 2>&1; then
  useradd --system --home /nonexistent --shell /usr/sbin/nologin torrimport
fi

# Add disabled Seerr Watchlist defaults to existing instance configs without
# overwriting API keys or any other user-managed settings.
for config in /etc/torr-arr-importer/radarr-1080.toml /etc/torr-arr-importer/sonarr-1080.toml; do
  [[ -f "$config" ]] || continue
  if ! grep -Eq '^[[:space:]]*seerr_watchlist_enabled[[:space:]]*=' "$config"; then
    cat >> "$config" <<'SEERR_CONFIG'

# Seerr Watchlist integration for terminal TorrServer failures (disabled until a real API key is entered).
seerr_watchlist_enabled = false
seerr_url = "http://192.168.0.14:5055"
seerr_api_key = "PUT_SEERR_API_KEY_HERE"
SEERR_CONFIG
    echo "Added disabled Seerr Watchlist settings to $config"
  fi
done

install -m 0755 "$SRC_DIR/src/importer.py" /opt/torr-arr-importer/importer.py
install -m 0644 "$SRC_DIR/systemd/torr-arr-importer@.service" /etc/systemd/system/torr-arr-importer@.service
install -m 0644 "$SRC_DIR/systemd/torr-arr-importer@.timer" /etc/systemd/system/torr-arr-importer@.timer
chown torrimport:torrimport /var/lib/torr-arr-importer /var/log/torr-arr-importer
chmod 0750 /var/lib/torr-arr-importer /var/log/torr-arr-importer

systemctl daemon-reload

echo "Installed torr-arr-importer v1.3.1"
echo "Create /etc/torr-arr-importer/radarr-1080.toml, then start:"
echo "  systemctl start torr-arr-importer@radarr-1080.service"
echo "  systemctl enable --now torr-arr-importer@radarr-1080.timer"
