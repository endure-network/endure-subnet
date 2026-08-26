#!/usr/bin/env bash
# scripts/dev/seed_chain.sh
#
# Idempotent seeder for a freshly-launched local subtensor chain:
#   1. Creates alice/owner/miner/validator wallets if missing
#   2. Funds owner/miner/validator from Alice if they're empty
#   3. Creates the endure-dev subnet if it doesn't exist yet
#      and captures the assigned netuid
#   4. Starts the subnet's emission schedule if not already started
#   5. Registers miner + validator on the subnet if not already registered
#   6. Stakes TAO from validator to itself if stake is below threshold
#
# Safe to re-run. Every step probes chain/filesystem state before acting,
# so a second invocation is close to a no-op.
#
# Design notes:
#   - No sentinel files. The handoff warned that stateful sentinels lie.
#     We re-derive everything from live chain/wallet state each run.
#   - Netuid is discovered by subnet_name, not hardcoded. If prior subnet
#     creation consumed earlier slots, the netuid may be 2, 3, etc.
#   - btcli ≥ 9.20 uses hyphenated flags; this script targets that version.
#
# Requires the chain to be up at ws://127.0.0.1:9946 (run_localnet.sh).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

BTCLI="${BTCLI:-$REPO_ROOT/.venv/bin/btcli}"
PY="${PY:-$REPO_ROOT/.venv/bin/python}"
WP="${WALLET_PATH:-$HOME/.bittensor/wallets}"
NET="${CHAIN_ENDPOINT:-ws://127.0.0.1:9946}"
SUBNET_NAME="${SUBNET_NAME:-endure-dev}"
OWNER_FUND_TAO="${OWNER_FUND_TAO:-100000}"
MINER_FUND_TAO="${MINER_FUND_TAO:-1000}"
VALIDATOR_FUND_TAO="${VALIDATOR_FUND_TAO:-10000}"
VALIDATOR_STAKE_TAO="${VALIDATOR_STAKE_TAO:-5000}"

if [[ ! -x "$BTCLI" ]]; then
  echo "missing: $BTCLI" >&2
  echo "hint: run '.venv/bin/pip install bittensor-cli'" >&2
  exit 1
fi

log() { printf '[seed_chain] %s\n' "$*" >&2; }

require_localnet() {
  local localnet_re='^wss?://(127\.0\.0\.1|localhost|\[::1\])(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$'
  if [[ "$NET" =~ $localnet_re ]]; then
    return
  fi
  log "REFUSING to run: configured endpoint is not a credential-free localnet address."
  log "It creates passwordless wallets and runs unsafe (no-MEV) chain ops;"
  log "it must only target a local dev chain (e.g. ws://127.0.0.1:9946)."
  exit 1
}

wallet_exists() {
  [[ -f "$WP/$1/coldkeypub.txt" ]]
}

hotkey_exists() {
  [[ -f "$WP/$1/hotkeys/$2" ]]
}

ss58_of() {
  "$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))['ss58Address'])" \
    "$WP/$1/coldkeypub.txt"
}

free_balance_tao() {
  "$BTCLI" wallet balance --wallet-name "$1" --wallet-path "$WP" \
    --network "$NET" --json-output 2>/dev/null \
    | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(next(iter(d.get('balances',{}).values()),{}).get('free',0))"
}

find_netuid_by_name() {
  "$BTCLI" subnets list --network "$NET" --json-output 2>/dev/null \
    | "$PY" -c "
import json, sys
d = json.load(sys.stdin).get('subnets', {})
for v in d.values():
    if isinstance(v, dict) and v.get('subnet_name') == sys.argv[1]:
        print(v['netuid']); break
" "$SUBNET_NAME"
}

neuron_uid() {
  local netuid="$1" hotkey_ss58="$2"
  "$BTCLI" wallet overview --wallet-name "$3" --wallet-path "$WP" \
    --network "$NET" --json-output 2>/dev/null \
    | "$PY" -c "
import json, sys
d = json.load(sys.stdin)
for sn in d.get('subnets', []):
    if sn.get('netuid') != int(sys.argv[1]): continue
    for n in sn.get('neurons', []):
        if n.get('hotkey_ss58') == sys.argv[2]:
            print(n.get('uid')); sys.exit(0)
" "$netuid" "$hotkey_ss58"
}

validator_stake_alpha() {
  local netuid="$1" hotkey_ss58="$2"
  "$BTCLI" wallet overview --wallet-name validator --wallet-path "$WP" \
    --network "$NET" --json-output 2>/dev/null \
    | "$PY" -c "
import json, sys
d = json.load(sys.stdin)
for sn in d.get('subnets', []):
    if sn.get('netuid') != int(sys.argv[1]): continue
    for n in sn.get('neurons', []):
        if n.get('hotkey_ss58') == sys.argv[2]:
            print(n.get('stake') or 0); sys.exit(0)
print(0)
" "$netuid" "$hotkey_ss58"
}

commit_reveal_weights_enabled() {
  # btcli sudo get needs the get_subnet_hyperparams_v3 runtime API, which the
  # localnet runtime image lacks (it returns a bare {"error": ...}); the SDK's
  # get_subnet_hyperparameters uses the older runtime call and works.
  "$PY" -c '
import sys
import bittensor as bt

hp = bt.Subtensor(network=sys.argv[2]).get_subnet_hyperparameters(int(sys.argv[1]))
print(str(None if hp is None else hp.commit_reveal_weights_enabled).lower())
' "$NETUID" "$NET" 2>/dev/null
}

disable_commit_reveal_weights_if_needed() {
  local enabled
  enabled="$(commit_reveal_weights_enabled)"
  if [[ "$enabled" == "false" ]]; then
    log "commit_reveal_weights_enabled already false on netuid=$NETUID"
    return
  fi
  # Localnet has no drand beacon, so CRv3 timelock commits cannot decrypt
  # reveals and accepted weight extrinsics never materialize in Weights storage.
  # The chain rejects admin ops during the protected weights window
  # (AdminActionProhibitedDuringWeightsWindow); with tempo=10 the admissible
  # window recurs every few blocks, so retry until it lands, then verify.
  log "disabling commit_reveal_weights_enabled on netuid=$NETUID for localnet"
  local result="" deadline
  deadline=$((SECONDS + ${COMMIT_REVEAL_DISABLE_TIMEOUT_SECONDS:-420}))
  while (( SECONDS < deadline )); do
    result="$("$BTCLI" sudo set --netuid "$NETUID" \
      --param commit_reveal_weights_enabled --value false \
      --wallet-name owner --wallet-path "$WP" --hotkey default \
      --network "$NET" --no-prompt --quiet --json-output 2>&1)" || true
    if grep -Eq '"success"[[:space:]]*:[[:space:]]*true' <<<"$result"; then
      break
    fi
    if ! grep -q "AdminActionProhibitedDuringWeightsWindow" <<<"$result"; then
      echo "[seed_chain] ERROR: failed to disable commit_reveal_weights_enabled on netuid=$NETUID; btcli returned a non-retryable error" >&2
      return 1
    fi
    sleep 3
  done
  if ! grep -Eq '"success"[[:space:]]*:[[:space:]]*true' <<<"$result"; then
    echo "[seed_chain] ERROR: timed out disabling commit_reveal_weights_enabled on netuid=$NETUID" >&2
    return 1
  fi
  # The accepted extrinsic lands in a later block; a verify read issued
  # immediately can race finalization and observe stale state, so retry.
  while (( SECONDS < deadline )); do
    if [[ "$(commit_reveal_weights_enabled)" == "false" ]]; then
      log "commit_reveal_weights_enabled disabled on netuid=$NETUID"
      return
    fi
    sleep 3
  done
  echo "[seed_chain] ERROR: timed out waiting for commit_reveal_weights_enabled=false on netuid=$NETUID after an accepted response" >&2
  return 1
}

ensure_coldkey() {
  local name="$1" uri="${2:-}"
  if wallet_exists "$name"; then
    log "wallet '$name' already exists, skipping"
    return
  fi
  if [[ -n "$uri" ]]; then
    "$BTCLI" wallet new-coldkey --wallet-name "$name" --wallet-path "$WP" \
      --uri "$uri" --no-use-password --quiet --json-output >/dev/null
  else
    "$BTCLI" wallet new-coldkey --wallet-name "$name" --wallet-path "$WP" \
      --n-words 12 --no-use-password --quiet --json-output >/dev/null
  fi
  log "created coldkey '$name' -> $(ss58_of "$name")"
}

ensure_hotkey() {
  local name="$1" hotkey="${2:-default}"
  if hotkey_exists "$name" "$hotkey"; then
    log "hotkey '$name/$hotkey' already exists, skipping"
    return
  fi
  "$BTCLI" wallet new-hotkey --wallet-name "$name" --wallet-path "$WP" \
    --hotkey "$hotkey" --n-words 12 --no-use-password --quiet --json-output >/dev/null
  log "created hotkey '$name/$hotkey'"
}

hotkey_ss58() {
  local name="$1" hotkey="${2:-default}"
  "$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))['ss58Address'])" \
    "$WP/$name/hotkeys/$hotkey"
}

transfer_if_empty() {
  local dest_name="$1" fund_tao="$2"
  local bal
  bal="$(free_balance_tao "$dest_name")"
  # "Empty" = <10 TAO, not "< fund_tao". Every extrinsic burns a small
  # fee, so a wallet we funded last run will drift below fund_tao before
  # next run. Without this guard the seeder would top up on every invocation,
  # which is convergent but not truly idempotent. 10 TAO is well above
  # any realistic accumulated fee drift, well below any usable working balance.
  if "$PY" -c "import sys; sys.exit(0 if float(sys.argv[1]) < 10 else 1)" "$bal"; then
    log "funding '$dest_name' (has $bal, fresh wallet) from alice with $fund_tao TAO"
    local dest_ss58
    dest_ss58="$(ss58_of "$dest_name")"
    "$BTCLI" wallet transfer --wallet-name alice --wallet-path "$WP" \
      --destination "$dest_ss58" --amount "$fund_tao" --network "$NET" \
      --no-prompt --quiet --json-output >/dev/null
    log "funded '$dest_name' with $fund_tao TAO"
  else
    log "wallet '$dest_name' already has $bal TAO, skipping fund"
  fi
}

# ---- Step 0: safety guard ----

require_localnet

# ---- Step 1: wallets ----

log "ensuring wallets exist in $WP"
ensure_coldkey alice Alice
ensure_coldkey owner
ensure_hotkey  owner default
ensure_coldkey miner
ensure_hotkey  miner default
ensure_coldkey validator
ensure_hotkey  validator default

# ---- Step 2: funding ----

log "funding owner/miner/validator from alice if needed"
transfer_if_empty owner     "$OWNER_FUND_TAO"
transfer_if_empty miner     "$MINER_FUND_TAO"
transfer_if_empty validator "$VALIDATOR_FUND_TAO"

# ---- Step 3: subnet create ----

NETUID="$(find_netuid_by_name)"
if [[ -n "${NETUID:-}" ]]; then
  log "subnet '$SUBNET_NAME' already exists at netuid=$NETUID, skipping create"
else
  log "creating subnet '$SUBNET_NAME' (this burns ~1000 TAO from owner)"
  "$BTCLI" subnets create \
    --wallet-name owner --wallet-path "$WP" --hotkey default \
    --subnet-name "$SUBNET_NAME" \
    --github-repo https://github.com/example/endure \
    --subnet-contact hello@endure.network \
    --subnet-url https://example.com \
    --discord-handle dev \
    --description 'dev subnet' \
    --logo-url https://example.com/logo.png \
    --additional-info none \
    --no-mev-protection \
    --network "$NET" --no-prompt >&2
  NETUID="$(find_netuid_by_name)"
  if [[ -z "${NETUID:-}" ]]; then
    log "ERROR: subnet create appeared to succeed but '$SUBNET_NAME' not found on chain"
    exit 1
  fi
  log "subnet '$SUBNET_NAME' created at netuid=$NETUID"
fi

# ---- Step 4: subnet start (emission schedule) ----

START_OUT="$("$BTCLI" subnets start --netuid "$NETUID" --wallet-name owner \
  --wallet-path "$WP" --hotkey default --network "$NET" --no-prompt 2>&1 || true)"
if echo "$START_OUT" | grep -q "already has an emission schedule"; then
  log "subnet $NETUID emission schedule already running"
elif echo "$START_OUT" | grep -qE "successfully|started"; then
  log "started subnet $NETUID emission schedule"
else
  log "subnet start returned unrecognized output; rerun btcli directly for details"
fi

disable_commit_reveal_weights_if_needed

# ---- Step 5: register miner + validator ----

register_if_missing() {
  local name="$1"
  local hk_ss58
  hk_ss58="$(hotkey_ss58 "$name")"
  local uid
  uid="$(neuron_uid "$NETUID" "$hk_ss58" "$name")"
  if [[ -n "${uid:-}" ]]; then
    log "'$name' already registered on netuid=$NETUID (uid=$uid), skipping"
    return
  fi
  log "registering '$name' on netuid=$NETUID"
  "$BTCLI" subnets register --netuid "$NETUID" \
    --wallet-name "$name" --wallet-path "$WP" --hotkey default \
    --network "$NET" --no-prompt --json-output >/dev/null
  uid="$(neuron_uid "$NETUID" "$hk_ss58" "$name")"
  log "registered '$name' on netuid=$NETUID (uid=${uid:-unknown})"
}

register_if_missing miner
register_if_missing validator

# ---- Step 6: validator stake ----

VAL_HK_SS58="$(hotkey_ss58 validator)"
CURRENT_STAKE="$(validator_stake_alpha "$NETUID" "$VAL_HK_SS58")"
if "$PY" -c "import sys; sys.exit(0 if float(sys.argv[1]) > 0 else 1)" "$CURRENT_STAKE"; then
  log "validator already has stake=$CURRENT_STAKE alpha on netuid=$NETUID, skipping"
else
  log "staking $VALIDATOR_STAKE_TAO TAO from validator on netuid=$NETUID"
  "$BTCLI" stake add --amount "$VALIDATOR_STAKE_TAO" --netuid "$NETUID" \
    --wallet-name validator --wallet-path "$WP" --hotkey default \
    --unsafe --no-mev-protection \
    --network "$NET" --no-prompt --json-output >/dev/null
  CURRENT_STAKE="$(validator_stake_alpha "$NETUID" "$VAL_HK_SS58")"
  log "validator stake now = $CURRENT_STAKE alpha"
fi

# ---- Summary ----

echo
echo "== chain seeded =="
echo "netuid:           $NETUID"
echo "owner:            $(ss58_of owner)"
echo "miner coldkey:    $(ss58_of miner)"
echo "miner hotkey:     $(hotkey_ss58 miner)"
echo "validator coldkey:$(ss58_of validator)"
echo "validator hotkey: $VAL_HK_SS58"
echo "validator stake:  $CURRENT_STAKE alpha"
echo
echo "next: launch neurons with --netuid $NETUID --subtensor.network $NET"
