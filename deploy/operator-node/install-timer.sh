#!/usr/bin/env bash
set -Eeuo pipefail

deploy_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly deploy_dir
readonly unit_dir="/etc/systemd/system"
readonly install_dir="/opt/endure-node"

if ((EUID != 0)); then
  echo "Run this installer as root." >&2
  exit 1
fi
if [[ "$deploy_dir" != "$install_dir" ]]; then
  echo "Install the operator files and .env in root-owned $install_dir first (see operator-node.md)." >&2
  exit 1
fi
python3 -I "$deploy_dir/check-installation.py" "$unit_dir" \
  "$deploy_dir/deploy.sh" "$deploy_dir/check-installation.py" \
  "$deploy_dir/docker-compose.yaml" "$deploy_dir/.env" \
  "$deploy_dir/endure-node-update.service" "$deploy_dir/endure-node-update.timer"
for state_path in /var/lib/endure-node /var/lib/endure-node/releases /var/lib/endure-node/backups; do
  if [[ -e "$state_path" || -L "$state_path" ]]; then
    python3 -I "$deploy_dir/check-installation.py" "$state_path"
  fi
done
# Replace destination entries atomically instead of following existing links.
service_tmp="$(mktemp "$unit_dir/.endure-service.XXXXXX")"
timer_tmp="$(mktemp "$unit_dir/.endure-timer.XXXXXX")"
trap 'rm -f -- "$service_tmp" "$timer_tmp"' EXIT
sed "s|@DEPLOY_DIR@|$deploy_dir|g" "$deploy_dir/endure-node-update.service" >"$service_tmp"
cat "$deploy_dir/endure-node-update.timer" >"$timer_tmp"
chmod 0644 "$service_tmp" "$timer_tmp"
mv -f -- "$service_tmp" "$unit_dir/endure-node-update.service"
mv -f -- "$timer_tmp" "$unit_dir/endure-node-update.timer"
systemctl daemon-reload
systemctl enable --now endure-node-update.timer
echo "Update timer enabled for $deploy_dir. Explicit image pins in .env remain pinned; inspect deploy.sh output for each service mode."
