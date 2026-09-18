#!/usr/bin/env bash
set -Eeuo pipefail

deploy_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly deploy_dir
readonly unit_dir="/etc/systemd/system"

if ((EUID != 0)); then
  echo "Run this installer as root." >&2
  exit 1
fi
# The service unit runs deploy.sh from this checkout, wherever it lives.
escaped_dir="$(printf '%s' "$deploy_dir" | sed -e 's/[\\&|]/\\&/g')"
sed -e "s|@DEPLOY_DIR@|$escaped_dir|g" "$deploy_dir/endure-node-update.service" \
  >"$unit_dir/endure-node-update.service"
install -m 0644 "$deploy_dir/endure-node-update.timer" "$unit_dir/endure-node-update.timer"
chmod 0644 "$unit_dir/endure-node-update.service"
systemctl daemon-reload
systemctl enable --now endure-node-update.timer
echo "This host now follows the Endure release channel from $deploy_dir."
