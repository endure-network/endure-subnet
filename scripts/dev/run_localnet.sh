#!/usr/bin/env bash
# scripts/dev/run_localnet.sh
#
# Minimal three-node subtensor localnet launcher.
#
# Why not just use subtensor/scripts/localnet.sh?
#   On this macOS host, libp2p mDNS discovery fails with
#   `failed to send mdns query error=IoError(HostUnreachable)`, which leaves
#   nodes stuck at 0 peers / block #0 forever. The upstream script has no
#   mDNS fallback, so we layer our own peering on top: deterministic node
#   keys in var/keys/, explicit --bootnodes, and --no-mdns.
#
# Assumptions:
#   * node-subtensor already built at subtensor/target/fast-runtime/release/
#     (run `bash subtensor/scripts/localnet.sh --build-only` once first, or
#      just `bash subtensor/scripts/localnet.sh` which also builds+runs)
#   * chainspec already generated at subtensor/scripts/specs/local.json
#     (same prerequisite — the build-only run writes it)
#   * var/keys/{one,two,three}.{key,peerid} already generated (run
#     scripts/dev/generate_node_keys.sh once)
#
# Foreground: runs until ctrl-C, streaming merged logs to stdout + var/chain.log.
# Cleanup: trap kills all three node processes.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

BIN="$REPO_ROOT/subtensor/target/fast-runtime/release/node-subtensor"
SPEC="$REPO_ROOT/subtensor/scripts/specs/local.json"
KEYDIR="$REPO_ROOT/var/keys"
LOG="$REPO_ROOT/var/chain.log"
NODE_BASE="$REPO_ROOT/var/localnet"

require() {
  if [[ ! -e "$1" ]]; then
    echo "missing: $1" >&2
    echo "hint: $2" >&2
    exit 1
  fi
}

require "$BIN"  "run 'bash subtensor/scripts/localnet.sh --build-only' first"
require "$SPEC" "run 'bash subtensor/scripts/localnet.sh --build-only' first"
require "$KEYDIR/one.key"     "run 'bash scripts/dev/generate_node_keys.sh' first"
require "$KEYDIR/one.peerid"  "run 'bash scripts/dev/generate_node_keys.sh' first"

mkdir -p "$(dirname "$LOG")"

# Fresh per-run: kill ONLY the localnet nodes this repo started (matched by
# our node base dir), never unrelated node-subtensor sessions, then purge
# their ephemeral chain state under repo-owned var/.
pkill -9 -f "node-subtensor.*$NODE_BASE" 2>/dev/null || true
sleep 1
rm -rf "$NODE_BASE"
mkdir -p "$NODE_BASE"

# Node key insertion for //Three is the only piece localnet.sh does that we
# can't skip: Three has no --three CLI shorthand in Substrate so we must
# insert its aura+grandpa keys manually.
"$BIN" key insert --base-path "$NODE_BASE/three" --chain "$SPEC" \
  --scheme Sr25519 --suri "//Three" --key-type aura
"$BIN" key insert --base-path "$NODE_BASE/three" --chain "$SPEC" \
  --scheme Ed25519 --suri "//Three" --key-type gran

ONE_PEERID="$(cat "$KEYDIR/one.peerid")"
TWO_PEERID="$(cat "$KEYDIR/two.peerid")"
THREE_PEERID="$(cat "$KEYDIR/three.peerid")"

ONE_BOOTNODE="/ip4/127.0.0.1/tcp/30334/p2p/$ONE_PEERID"
TWO_BOOTNODE="/ip4/127.0.0.1/tcp/30335/p2p/$TWO_PEERID"
THREE_BOOTNODE="/ip4/127.0.0.1/tcp/30336/p2p/$THREE_PEERID"

common=(
  --chain="$SPEC"
  --rpc-cors=all
  --allow-private-ipv4
  --validator
  --no-mdns
)

one_args=(
  --base-path "$NODE_BASE/one" --one
  --port 30334 --rpc-port 9944
  --node-key-file "$KEYDIR/one.key"
  --bootnodes "$TWO_BOOTNODE" --bootnodes "$THREE_BOOTNODE"
  "${common[@]}"
)

two_args=(
  --base-path "$NODE_BASE/two" --two
  --port 30335 --rpc-port 9945
  --node-key-file "$KEYDIR/two.key"
  --bootnodes "$ONE_BOOTNODE" --bootnodes "$THREE_BOOTNODE"
  "${common[@]}"
)

three_args=(
  --base-path "$NODE_BASE/three" --name Three
  --port 30336 --rpc-port 9946
  --node-key-file "$KEYDIR/three.key"
  --bootnodes "$ONE_BOOTNODE" --bootnodes "$TWO_BOOTNODE"
  "${common[@]}"
)

trap 'echo "[run_localnet] stopping nodes..."; pkill -P $$ 2>/dev/null || true' EXIT INT TERM

echo "[run_localnet] logs: $LOG"
echo "[run_localnet] starting one/two/three with --no-mdns + explicit bootnodes"

: >"$LOG"
(
  "$BIN" "${one_args[@]}"   2>&1 | sed 's/^/[one] /'   &
  "$BIN" "${two_args[@]}"   2>&1 | sed 's/^/[two] /'   &
  "$BIN" "${three_args[@]}" 2>&1 | sed 's/^/[three] /'
  wait
) | tee -a "$LOG"
