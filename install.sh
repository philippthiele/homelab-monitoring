#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "$REPO_DIR/.env" ]]; then
    cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
    chmod 600 "$REPO_DIR/.env"
    echo "Created $REPO_DIR/.env"
    echo "Fill in TELEGRAM_BOT_TOKEN, then run:"
    echo "  python3 $REPO_DIR/monitor.py --setup-telegram"
fi

chmod 600 "$REPO_DIR/.env"
mkdir -p "$REPO_DIR/data"

for unit in \
    homelab-monitoring.service \
    homelab-monitoring-failure.service \
    homelab-monitoring-weekly.service \
    homelab-monitoring-weekly.timer
do
    sudo ln -sf "$REPO_DIR/systemd/$unit" "/etc/systemd/system/$unit"
done

sudo systemctl daemon-reload
sudo systemctl enable --now homelab-monitoring.service
sudo systemctl enable --now homelab-monitoring-weekly.timer

echo
echo "Installed. Status:"
sudo systemctl --no-pager --full status homelab-monitoring.service || true
echo
echo "Dashboard: http://192.168.178.62:8090"
echo "Weekly timer: $(systemctl list-timers homelab-monitoring-weekly.timer --no-pager --no-legend)"
