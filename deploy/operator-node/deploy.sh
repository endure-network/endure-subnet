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
readonly pending_file="$release_dir/pending-deployment"
readonly retry_file="$release_dir/backup-retry-after"
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
readonly images_program='import json
import sys

services = json.load(sys.stdin)["services"].values()
print("\n".join(sorted({service["image"] for service in services})))'
readonly backup_program='import os
import sqlite3

source_path = os.environ["SOURCE_PATH"]
backup_path = os.environ["BACKUP_PATH"]
# One fixed in-volume name: a run that died before its cleanup is overwritten
# by the next one instead of adding another full copy to the data volume.
if os.path.exists(backup_path):
    os.unlink(backup_path)
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

for required in docker python3 realpath flock sha256sum curl; do
  if ! command -v "$required" >/dev/null 2>&1; then
    echo "Missing deployment prerequisite: $required" >&2
    exit 1
  fi
done

if [[ ! -r "$env_file" ]]; then
  echo "Missing deployment environment: $env_file" >&2
  exit 1
fi

# Rendered once: this runs from a timer, and every value below is read from it.
config_json="$("${compose[@]}" config --format json)"
readonly config_json
validator_wallet_root="$(python3 -c "$wallet_source_program" validator <<<"$config_json")"
miner_wallet_root="$(python3 -c "$wallet_source_program" miner-1 <<<"$config_json")"
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
if [[ -f "$pending_file" ]]; then
  echo "Unfinished deployment requires maintenance recovery: $(cat "$pending_file")" >&2
  echo "Verify or restore that release under deploy.lock before clearing pending-deployment." >&2
  exit 1
fi
mapfile -t images < <(python3 -c "$images_program" <<<"$config_json")
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

for service in validator miner-1; do
  configured_image="$(python3 -c "$service_image_program" "$service" <<<"$config_json")"
  case "$configured_image" in
    "ghcr.io/endure-network/endure-subnet-validator:$serving_stage"|"ghcr.io/endure-network/endure-subnet-miner:$serving_stage")
      echo "$service follows :$serving_stage." ;;
    *) echo "$service uses an explicit image override: $configured_image" ;;
  esac
done

for image in "${images[@]}"; do
  docker pull "$image"
done
validator_image="$(python3 -c "$service_image_program" validator <<<"$config_json")"
miner_image="$(python3 -c "$service_image_program" miner-1 <<<"$config_json")"
validator_image_id="$(docker image inspect --format '{{.Id}}' "$validator_image")"
miner_image_id="$(docker image inspect --format '{{.Id}}' "$miner_image")"
readonly validator_image validator_image_id miner_image miner_image_id

image_revision() {
  docker image inspect \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$1"
}
revision="$(image_revision "$validator_image_id")"
miner_revision="$(image_revision "$miner_image_id")"
readonly revision miner_revision
# The release job moves the two channel tags one after the other, so a host
# that polls in between sees a new validator with the old miner. Two digest
# pins from different releases look the same.
if [[ -z "$revision" || "$revision" != "$miner_revision" ]]; then
  echo "Validator ($revision) and miner ($miner_revision) images come from different commits." >&2
  echo "A channel that is mid-release settles by the next run; pinned images must name one release." >&2
  exit 1
fi

# From here every compose call names the pulled image IDs, so a tag that moves
# in the local store during the run cannot change what is compared, started,
# or recorded. Compose hashes the image string, so the hashes are taken under
# the same override the containers are started with.
export VALIDATOR_IMAGE="$validator_image_id" MINER_IMAGE="$miner_image_id"
config_hashes="$("${compose[@]}" config --hash '*')"
readonly config_hashes
service_hash() {
  awk -v s="$1" '$1 == s {print $2}' <<<"$config_hashes"
}
# The hash covers the image ID and the rendered env file, so a release that
# failed under a bad env edit is a different identity once the edit is fixed.
release_identity="$(service_hash validator) $(service_hash miner-1)"
readonly release_identity

# After a failed release the channel tag still resolves to it. Without this the
# next timer run would deploy it, fail, and roll back again, every interval.
if [[ -f "$rejected_file" ]] && grep -Fxq -- "$release_identity" "$rejected_file"; then
  echo "Release $revision failed its health gate on this host with this configuration." >&2
  echo "Waiting for the next release or env change; remove $rejected_file to retry." >&2
  exit 1
fi

wait_for_healthy() {
  local service="$1"
  local container_id status
  container_id="$("${compose[@]}" ps -aq "$service")"
  [[ -n "$container_id" ]] || return 1
  [[ "$(docker inspect --format '{{.State.Running}}' "$container_id")" == "true" ]] || return 1
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

service_is_current() {
  local service="$1" wanted_image_id="$2"
  local container_id
  container_id="$("${compose[@]}" ps -aq "$service")"
  [[ -n "$container_id" ]] || return 1
  # Compare identity here; health is checked separately. An identical stopped
  # service is reported, never implicitly restarted.
  [[ "$(docker inspect --format '{{.Image}}' "$container_id")" == "$wanted_image_id" ]] \
    || return 1
  # An edited env file changes this hash, so set-once values still apply.
  [[ "$(docker inspect --format '{{index .Config.Labels "com.docker.compose.config-hash"}}' "$container_id")" == "$(service_hash "$service")" ]]
}

if service_is_current validator "$validator_image_id" \
  && service_is_current miner-1 "$miner_image_id"; then
  if ! wait_for_healthy validator || ! wait_for_healthy miner-1; then
    echo "Current services are stopped or unhealthy; left unchanged." >&2
    exit 1
  fi
  echo "Revision $revision is already deployed; nothing to do."
  exit 0
fi

validator_id="$("${compose[@]}" ps -aq validator)"
if [[ -z "$validator_id" ]] && docker volume inspect "$state_volume" >/dev/null 2>&1; then
  echo "Existing validator state cannot be backed up without its container." >&2
  exit 1
fi

# A run that fails before anything is started changed nothing. Under the timer
# it repeats every interval, so it must not leave a record or a snapshot.
if [[ -f "$retry_file" ]] && (( $(date +%s) < $(cat "$retry_file") )); then
  echo "Backup retry deferred for up to one hour; remove $retry_file after fixing the failure to retry now." >&2
  exit 1
fi
deploy_started=0
backup_file=""
backup_inside="/data/.predeploy.db"
cleanup_snapshot() {
  [[ -n "$validator_id" ]] || return 0
  if [[ "$(docker inspect --format '{{.State.Running}}' "$validator_id")" == "true" ]]; then
    docker exec -e BACKUP_PATH="$backup_inside" "$validator_id" python -c \
      'import os; p=os.environ["BACKUP_PATH"]; os.path.exists(p) and os.unlink(p)'
  else
    local snapshot_image
    snapshot_image="$(docker inspect --format '{{.Image}}' "$validator_id")"
    docker run --rm --entrypoint python --volumes-from "$validator_id" \
      -e BACKUP_PATH="$backup_inside" "$snapshot_image" -c \
      'import os; p=os.environ["BACKUP_PATH"]; os.path.exists(p) and os.unlink(p)'
  fi
}
discard_unstarted_run() {
  if ((deploy_started == 0)); then
    cleanup_snapshot >/dev/null 2>&1 || true
    rm -rf -- "$record_dir"
    [[ -z "$backup_file" ]] || rm -f -- "$backup_file"
  fi
}
trap discard_unstarted_run EXIT
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

if [[ -n "$validator_id" ]]; then
  printf '%s\n' "$(( $(date +%s) + 3600 ))" >"$retry_file"
  backup_file="$backup_dir/validator-predeploy-$timestamp.db"
  if [[ "$(docker inspect --format '{{.State.Running}}' "$validator_id")" == "true" ]]; then
    docker exec -i \
      -e SOURCE_PATH=/data/validator-live.db \
      -e BACKUP_PATH="$backup_inside" \
      "$validator_id" python -c "$backup_program"
    docker cp "$validator_id:$backup_inside" "$backup_file"
  else
    stopped_validator_image_id="$(docker inspect --format '{{.Image}}' "$validator_id")"
    docker run --rm --entrypoint python --volumes-from "$validator_id" \
      -e SOURCE_PATH=/data/validator-live.db \
      -e BACKUP_PATH="$backup_inside" \
      "$stopped_validator_image_id" -c "$backup_program"
    docker cp "$validator_id:$backup_inside" "$backup_file"
  fi
  cleanup_snapshot
  chmod 0600 "$backup_file"
  printf '%s\n' "$backup_file" >"$record_dir/backup-path.txt"
  sha256sum "$backup_file" >"$record_dir/backup.sha256"
fi


rm -f -- "$retry_file"
if [[ ! -f "$release_dir/retention-initialized" ]]; then
  { cat "$release_dir"/*/previous-images.txt 2>/dev/null || true; } \
    | awk -F'|' '{print $3}' >>"$release_dir/protected-images.txt"
  find "$backup_dir" -maxdepth 1 -name 'validator-predeploy-*.db' \
    >>"$release_dir/protected-backups.txt"
  touch "$record_dir/protected" "$release_dir/retention-initialized"
fi
printf '%s\n' "$validator_image_id" "$miner_image_id" >>"$release_dir/managed-images.txt"
printf '%s\n' "$release_identity" >"$record_dir/target-identity.txt"
printf '%s\n' "$validator_image_id" "$miner_image_id" >"$record_dir/target-images.txt"
touch "$record_dir/managed"


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
  restore_inside="/data/.rollback.db"
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
  if [[ "$previous_validator_image_id" == "$validator_image_id" && "$previous_miner_image_id" == "$miner_image_id" ]]; then
    # The same configuration just passed health on rollback; do not poison it.
    awk -v identity="$release_identity" '$0 != identity' "$rejected_file" >"$rejected_file.tmp"
    mv "$rejected_file.tmp" "$rejected_file"
  fi
  touch "$record_dir/rollback-complete"
  rm -f -- "$pending_file"
  echo "Previous validator and miner images restored after failed deployment." >&2
}

deploy_started=1
printf '%s\n' "$record_dir" >"$pending_file.tmp"
mv "$pending_file.tmp" "$pending_file"
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
  for image in "$validator_image|$validator_image_id" "$miner_image|$miner_image_id"; do
    printf 'IMAGE=%s|%s\n' "$image" \
      "$(docker image inspect --format '{{join .RepoDigests ","}}' "${image#*|}")"
  done
  for service in validator miner-1; do
    container_id="$("${compose[@]}" ps -q "$service")"
    docker inspect --format '{{.Name}}|{{.Config.Image}}|{{.Image}}|{{.State.StartedAt}}' "$container_id"
  done
} >"$record_dir/deployment.txt"
rm -f -- "$pending_file"

# Keep the adoption baseline plus current/previous images and recent snapshots.
# Only inventory written by this deployer is eligible for automatic cleanup.
keep_image_ids=" $validator_image_id $miner_image_id $previous_validator_image_id $previous_miner_image_id "
while IFS= read -r superseded_image_id; do
  [[ -n "$superseded_image_id" ]] || continue
  if [[ "$keep_image_ids" != *" $superseded_image_id "* ]] \
    && ! grep -Fxq -- "$superseded_image_id" "$release_dir/protected-images.txt"; then
    docker image rm "$superseded_image_id" >/dev/null 2>&1 || true
  fi
done < <(sort -u "$release_dir/managed-images.txt")
mapfile -t snapshots < <(find "$backup_dir" -maxdepth 1 -name 'validator-predeploy-*.db' | sort)
for ((i = 0; i < ${#snapshots[@]} - 3; i++)); do
  if ! grep -Fxq -- "${snapshots[i]}" "$release_dir/protected-backups.txt"; then
    rm -f -- "${snapshots[i]}"
  fi
done
mapfile -t records < <(find "$release_dir" -mindepth 1 -maxdepth 1 -type d | sort)
for ((i = 0; i < ${#records[@]} - 20; i++)); do
  record="${records[i]}"
  if [[ -f "$record/managed" && ! -f "$record/protected" ]] \
    && [[ -f "$record/deployment.txt" || -f "$record/rollback-complete" ]]; then
    rm -rf -- "$record"
  fi
done

echo "Deployed revision $revision and passed process health checks."
echo "Complete the lifecycle and chain-side verification in docs/deploy/operator-node.md."
