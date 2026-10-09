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

install -m 0755 "$SRC_DIR/src/importer.py" /opt/torr-arr-importer/importer.py
install -m 0644 "$SRC_DIR/systemd/torr-arr-importer@.service" /etc/systemd/system/torr-arr-importer@.service
install -m 0644 "$SRC_DIR/systemd/torr-arr-importer@.timer" /etc/systemd/system/torr-arr-importer@.timer
chown torrimport:torrimport /var/lib/torr-arr-importer /var/log/torr-arr-importer
chmod 0750 /var/lib/torr-arr-importer /var/log/torr-arr-importer

systemctl daemon-reload

echo "Installed torr-arr-importer v1.0.3"
echo "Create /etc/torr-arr-importer/radarr-1080.toml, then start:"
echo "  systemctl start torr-arr-importer@radarr-1080.service"
echo "  systemctl enable --now torr-arr-importer@radarr-1080.timer"
