#!/usr/bin/env bash
# scripts/dev/generate_node_keys.sh
#
# Generate deterministic libp2p node keys for the three-node localnet.
# Writes var/keys/{one,two,three}.key (secret hex) and .peerid (12D3...).
#
# Run once per workstation. Idempotent: will regenerate if re-run.
# The keys are dev-only and never leave this machine — they're the
# equivalent of //Alice-style known keys for p2p peering.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

BIN="$REPO_ROOT/subtensor/target/fast-runtime/release/node-subtensor"
SPEC="$REPO_ROOT/subtensor/scripts/specs/local.json"
KEYDIR="$REPO_ROOT/var/keys"

if [[ ! -x "$BIN" ]]; then
  echo "missing: $BIN" >&2
  echo "hint: run 'bash subtensor/scripts/localnet.sh --build-only' first" >&2
  exit 1
fi

if [[ ! -f "$SPEC" ]]; then
  echo "missing: $SPEC" >&2
  echo "hint: run 'bash subtensor/scripts/localnet.sh --build-only' first" >&2
  exit 1
fi

mkdir -p "$KEYDIR"

for name in one two three; do
  rm -f "$KEYDIR/$name.key"
  peerid="$("$BIN" key generate-node-key \
    --file "$KEYDIR/$name.key" --chain "$SPEC" 2>&1 >/dev/null)"
  printf '%s\n' "$peerid" > "$KEYDIR/$name.peerid"
  echo "generated $name: $peerid"
done
