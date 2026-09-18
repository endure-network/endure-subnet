#!/usr/bin/env bash
set -Eeuo pipefail

deploy_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly deploy_dir
readonly env_file="${ENDURE_ENV_FILE:-$deploy_dir/.env}"
readonly backup_dir="/var/lib/endure-node/backups"
readonly release_dir="/var/lib/endure-node/releases"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
readonly timestamp
readonly record_dir="$release_dir/$timestamp"
readonly rejected_file="$release_dir/rejected-releases.txt"
readonly compose_file="$deploy_dir/docker-compose.yaml"
readonly -a compose=(docker compose --env-file "$env_file" -f "$compose_file")
readonly state_volume="endure-subnet_validator-data"
readonly wallet_source_program='import json
import sys

config = json.load(sys.stdin)
service = config["services"][sys.argv[1]]
matches = [
    volume["source"]
    for volume in service["volumes"]
    if volume["target"] == "/root/.bittensor/wallets"
]
if len(matches) != 1:
    raise SystemExit(f"expected one wallet mount for {sys.argv[1]}")
print(matches[0])'
readonly service_image_program='import json
import sys

print(json.load(sys.stdin)["services"][sys.argv[1]]["image"])'
readonly backup_program='import os
import sqlite3

source_path = os.environ["SOURCE_PATH"]
backup_path = os.environ["BACKUP_PATH"]
if not os.path.isfile(source_path):
    raise SystemExit("validator database is missing")
if os.path.getsize(source_path) == 0:
    raise SystemExit("validator database is empty")
with sqlite3.connect(source_path) as source, sqlite3.connect(backup_path) as target:
    source.backup(target)
    result = target.execute("PRAGMA integrity_check").fetchone()
if result != ("ok",):
    raise SystemExit(f"backup integrity check failed: {result!r}")'
readonly restore_program='import os
import shutil

source_path = os.environ["RESTORE_SOURCE"]
target_path = "/data/validator-live.db"
for suffix in ("-wal", "-shm"):
    try:
        os.unlink(target_path + suffix)
    except FileNotFoundError:
        pass
shutil.copyfile(source_path, target_path)
os.unlink(source_path)'

if [[ ! -r "$env_file" ]]; then
  echo "Missing deployment environment: $env_file" >&2
  exit 1
fi

"${compose[@]}" config --quiet
validator_wallet_root="$(
  "${compose[@]}" config --format json \
    | python3 -c "$wallet_source_program" validator
)"
miner_wallet_root="$(
  "${compose[@]}" config --format json \
    | python3 -c "$wallet_source_program" miner-1
)"
validator_wallet_root="$(realpath "$validator_wallet_root")"
miner_wallet_root="$(realpath "$miner_wallet_root")"
if [[ "$validator_wallet_root" == "$miner_wallet_root" \
  || "$validator_wallet_root" == "$miner_wallet_root/"* \
  || "$miner_wallet_root" == "$validator_wallet_root/"* ]]; then
  echo "Validator and miner wallet roots must be separate, non-overlapping directories." >&2
  exit 1
fi
if ((EUID != 0)); then
  echo "Run this deployment as root." >&2
  exit 1
fi
install -d -o root -g root -m 0700 "$backup_dir" "$release_dir"
umask 077
exec 9>"$release_dir/deploy.lock"
if ! flock -n 9; then
  echo "Another Endure deployment is already running." >&2
  exit 1
fi
mapfile -t images < <("${compose[@]}" config --images | sort -u)
if ((${#images[@]} != 2)); then
  echo "Expected exactly two runtime images, found ${#images[@]}." >&2
  exit 1
fi
serving_stage="$(awk -F= '$1 == "SERVING_STAGE" {print $2; exit}' "$env_file")"
if [[ "$serving_stage" != "testnet" && "$serving_stage" != "mainnet" ]]; then
  echo "SERVING_STAGE must be testnet or mainnet (got '$serving_stage')." >&2
  exit 1
fi
# The neurons refuse to serve when SERVING_STAGE does not match CHAIN
# (endure/utils/config.py require_serving_stage_allowed); that runtime gate,
# not this script, is the mainnet authority.

# This is the only pull. Everything below works from the image IDs it resolved
# and starts containers with --pull never, so a channel tag that moves during
# the run cannot change what gets started or recorded.
for image in "${images[@]}"; do
  docker pull "$image"
done
validator_image="$(
  "${compose[@]}" config --format json \
    | python3 -c "$service_image_program" validator
)"
miner_image="$(
  "${compose[@]}" config --format json \
    | python3 -c "$service_image_program" miner-1
)"
validator_image_id="$(docker image inspect --format '{{.Id}}' "$validator_image")"
miner_image_id="$(docker image inspect --format '{{.Id}}' "$miner_image")"
revision="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$validator_image_id")"
readonly release_identity="$validator_image_id $miner_image_id"

# After a failed release the channel tag still resolves to it. Without this the
# next timer run would deploy it, fail, and roll back again, every interval.
if [[ -f "$rejected_file" ]] && grep -Fxq -- "$release_identity" "$rejected_file"; then
  echo "Release $revision failed its health gate on this host and was rolled back." >&2
  echo "Waiting for the next release; remove $rejected_file to retry this one." >&2
  exit 1
fi

service_is_current() {
  local service="$1" wanted_image_id="$2"
  local container_id wanted_hash
  container_id="$("${compose[@]}" ps -aq "$service")"
  [[ -n "$container_id" ]] || return 1
  [[ "$(docker inspect --format '{{.State.Running}}' "$container_id")" == "true" ]] \
    || return 1
  [[ "$(docker inspect --format '{{.Image}}' "$container_id")" == "$wanted_image_id" ]] \
    || return 1
  # An edited env file changes this hash, so set-once values still apply.
  wanted_hash="$("${compose[@]}" config --hash '*' | awk -v s="$service" '$1 == s {print $2}')"
  [[ "$(docker inspect --format '{{index .Config.Labels "com.docker.compose.config-hash"}}' "$container_id")" == "$wanted_hash" ]]
}

if service_is_current validator "$validator_image_id" \
  && service_is_current miner-1 "$miner_image_id"; then
  echo "Already running revision $revision; nothing to deploy."
  exit 0
fi

install -d -o root -g root -m 0700 "$record_dir"
previous_validator_image_id=""
previous_miner_image_id=""
for service in validator miner-1; do
  container_id="$("${compose[@]}" ps -aq "$service")"
  if [[ -n "$container_id" ]]; then
    image_ref="$(docker inspect --format '{{.Config.Image}}' "$container_id")"
    image_id="$(docker inspect --format '{{.Image}}' "$container_id")"
    printf '%s|%s|%s\n' "$service" "$image_ref" "$image_id" \
      >>"$record_dir/previous-images.txt"
    if [[ "$service" == "validator" ]]; then
      previous_validator_image_id="$image_id"
    else
      previous_miner_image_id="$image_id"
    fi
  fi
done

backup_file=""
validator_id="$("${compose[@]}" ps -aq validator)"
if [[ -n "$validator_id" ]]; then
  backup_inside="/data/.predeploy-$timestamp.db"
  backup_file="$backup_dir/validator-predeploy-$timestamp.db"
  if [[ "$(docker inspect --format '{{.State.Running}}' "$validator_id")" == "true" ]]; then
    docker exec -i \
      -e SOURCE_PATH=/data/validator-live.db \
      -e BACKUP_PATH="$backup_inside" \
      "$validator_id" python -c "$backup_program"
    docker cp "$validator_id:$backup_inside" "$backup_file"
    docker exec -e BACKUP_PATH="$backup_inside" "$validator_id" python -c \
      'import os; os.unlink(os.environ["BACKUP_PATH"])'
  else
    stopped_validator_image_id="$(docker inspect --format '{{.Image}}' "$validator_id")"
    docker run --rm --entrypoint python --volumes-from "$validator_id" \
      -e SOURCE_PATH=/data/validator-live.db \
      -e BACKUP_PATH="$backup_inside" \
      "$stopped_validator_image_id" -c "$backup_program"
    docker cp "$validator_id:$backup_inside" "$backup_file"
    docker run --rm --entrypoint python --volumes-from "$validator_id" \
      -e BACKUP_PATH="$backup_inside" \
      "$stopped_validator_image_id" -c \
      'import os; os.unlink(os.environ["BACKUP_PATH"])'
  fi
  chmod 0600 "$backup_file"
  printf '%s\n' "$backup_file" >"$record_dir/backup-path.txt"
  sha256sum "$backup_file" >"$record_dir/backup.sha256"
elif docker volume inspect "$state_volume" >/dev/null 2>&1; then
  echo "Existing validator state cannot be backed up without its container." >&2
  exit 1
fi

wait_for_healthy() {
  local service="$1"
  local container_id status
  container_id="$("${compose[@]}" ps -q "$service")"
  for _ in $(seq 1 48); do
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")"
    if [[ "$status" == "healthy" ]]; then
      return 0
    fi
    if [[ "$status" == "unhealthy" || "$status" == "exited" || "$status" == "dead" ]]; then
      echo "$service entered state $status." >&2
      return 1
    fi
    sleep 5
  done
  echo "$service did not become healthy within 240 seconds." >&2
  return 1
}

rollback_failed_release() {
  echo "New release failed health checks; attempting automatic rollback." >&2
  printf '%s\n' "$release_identity" >>"$rejected_file"
  "${compose[@]}" stop validator miner-1 || return 1
  if [[ -z "$previous_validator_image_id" || -z "$previous_miner_image_id" || -z "$backup_file" ]]; then
    echo "Automatic rollback is unavailable; the new services remain stopped." >&2
    return 1
  fi
  sha256sum --check "$record_dir/backup.sha256" || return 1
  current_validator_id="$("${compose[@]}" ps -aq validator)" || return 1
  if [[ -z "$current_validator_id" ]]; then
    echo "Automatic rollback cannot access the validator data volume." >&2
    return 1
  fi
  current_validator_image="$(docker inspect --format '{{.Image}}' "$current_validator_id")" \
    || return 1
  restore_inside="/data/.rollback-$timestamp.db"
  docker cp "$backup_file" "$current_validator_id:$restore_inside" || return 1
  docker run --rm --entrypoint python --volumes-from "$current_validator_id" \
    -e RESTORE_SOURCE="$restore_inside" \
    "$current_validator_image" -c "$restore_program" || return 1
  # The channel tag now resolves to the failed release; only the recorded
  # local image IDs still identify the previous one.
  VALIDATOR_IMAGE="$previous_validator_image_id" \
    MINER_IMAGE="$previous_miner_image_id" \
    "${compose[@]}" up -d --no-build --pull never validator miner-1 || {
    "${compose[@]}" stop validator miner-1 || true
    return 1
  }
  if ! wait_for_healthy validator || ! wait_for_healthy miner-1; then
    "${compose[@]}" stop validator miner-1 || true
    return 1
  fi
  echo "Previous validator and miner images restored after failed deployment." >&2
}

if ! "${compose[@]}" up -d --no-build --pull never validator miner-1; then
  rollback_failed_release || true
  exit 1
fi

if ! wait_for_healthy validator \
  || ! wait_for_healthy miner-1 \
  || ! curl --fail --silent --show-error --retry 5 --retry-all-errors \
    --retry-delay 2 --connect-timeout 5 --max-time 10 \
    http://127.0.0.1:8714/live >"$record_dir/live.json"; then
  rollback_failed_release || true
  exit 1
fi
curl --silent --show-error --output "$record_dir/health.json" \
  --write-out '%{http_code}\n' --connect-timeout 5 --max-time 10 \
  http://127.0.0.1:8714/health \
  >"$record_dir/health-status.txt"

{
  printf 'REVISION=%s\n' "$revision"
  for image in "$validator_image" "$miner_image"; do
    printf 'IMAGE=%s|%s|%s\n' "$image" \
      "$(docker image inspect --format '{{.Id}}' "$image")" \
      "$(docker image inspect --format '{{join .RepoDigests ","}}' "$image")"
  done
  for service in validator miner-1; do
    container_id="$("${compose[@]}" ps -q "$service")"
    docker inspect --format '{{.Name}}|{{.Config.Image}}|{{.Image}}|{{.State.StartedAt}}' "$container_id"
  done
} >"$record_dir/deployment.txt"

echo "Deployed revision $revision and passed process health checks."
echo "Complete the lifecycle and chain-side verification in docs/deploy/operator-node.md."
