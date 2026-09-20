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
paths=("$unit_dir" "$deploy_dir/deploy.sh" "$deploy_dir/install-timer.sh"
       "$deploy_dir/docker-compose.yaml" "$deploy_dir/.env"
       "$deploy_dir/endure-node-update.service" "$deploy_dir/endure-node-update.timer")
for state_path in /var/lib/endure-node /var/lib/endure-node/releases /var/lib/endure-node/backups; do
  if [[ -e "$state_path" || -L "$state_path" ]]; then paths+=("$state_path"); fi
done
# Keep validation in this explicitly invoked installer; do not execute a sibling
# helper before establishing ownership of the installation.
python3 -I - "${paths[@]}" <<'PY_CHECK'
"""Validate the administrator-controlled files used by the system timer."""

import os
import stat
import sys
from pathlib import Path


def validate_path(path: Path) -> None:
    """Reject replaceable files, links, and ancestor directories."""
    path = path.absolute()
    for component in (path, *path.parents):
        metadata = component.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & 0o022
        ):
            raise ValueError(f"Unsafe installation path: {component}")


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Installation validation requires root.")
    try:
        for argument in sys.argv[1:]:
            validate_path(Path(argument))
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
PY_CHECK
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
