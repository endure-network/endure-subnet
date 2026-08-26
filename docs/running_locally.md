# Running Endure Locally

End-to-end instructions for bringing up a local Endure subnet on macOS: a
three-node subtensor chain, a seeded subnet with miner + validator wallets,
and the Endure neuron entrypoints with the validator successfully writing
weights to chain.

**Scope.** Developer ergonomics only. Alpha Risk V1 is the default served
runtime; Forge lending remains selectable as a reference schema, and lending is
registered/selectable but not served yet.

Written against macOS on Apple Silicon. Linux steps are noted where they
differ.

## Prerequisites

These tools must be present before anything else:

```bash
brew install git tmux protobuf llvm openssl@3
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

Then ensure cargo is on `PATH`. On this machine, `rustup` was installed via
Homebrew, which puts the toolchain under `~/.rustup/toolchains/...` but
**does not symlink `cargo` onto PATH**. Export it for every shell that
builds subtensor:

```bash
export PATH="$HOME/.rustup/toolchains/stable-aarch64-apple-darwin/bin:/opt/homebrew/opt/rustup/bin:$PATH"
cargo --version   # sanity-check; should print cargo 1.95+
```

Install the Rust toolchain components subtensor needs:

```bash
rustup toolchain install stable
rustup target add wasm32v1-none
```

Python environment — Endure supports Python 3.12 only (`>=3.12,<3.13`). From
the repository root, bootstrap the required uv 0.11.32 and install the locked
runtime environment:

On Debian or Ubuntu, install `python3.12-venv` before running `make bootstrap`:

```bash
sudo apt install python3.12-venv
```

```bash
make bootstrap
make install         # uv sync --locked --no-dev, creating .venv as needed
make seeder-install  # hash-locked btcli for scripts/dev/seed_chain.sh
```

`make seeder-install` builds a separate `.venv-seeder` on purpose: `btcli` and
its dependency tree are pinned independently of the Endure lockfile, so the
seeder toolchain never has to agree with `.venv`.

Use `make dev-install` (`uv sync --locked --extra dev`) when you need the test,
lint, and type-check tooling. `make bootstrap` is the documented installation
path for uv and Gitleaks and installs them into the local user cache; the
locked Endure commands do not use another tool from `PATH`. Do not use `pip
install` for the Endure package.

### Mock smoke processes

After `make dev-install`, use separate temporary homes for the mock validator
and miner. Both targets retain the caller's `HOME`; this keeps their generated
wallet and runtime state isolated from your normal Bittensor state:

```bash
HOME=/tmp/endure-public-validator timeout 90s make dev
# From another shell within 60 seconds:
curl --fail http://127.0.0.1:8714/live
curl --fail http://127.0.0.1:8714/health

# Run after the validator exits:
HOME=/tmp/endure-public-miner timeout 90s make dev-miner
```

The validator liveness response reports `status: live` and its readiness response
reports `status: ok`; the miner logs `Miner running...` when it reaches steady
state. Remove both temporary homes after the timed runs.

On macOS the `timeout` command is not installed by default; install GNU
coreutils (`brew install coreutils`) and use `gtimeout`, or drop the `timeout`
prefix and stop each process manually.

## Fast path — chain from a container (skips Phases 1–2)

Phases 1–2 build `node-subtensor` from source (~7 min warm, ~20 min cold) and
launch it with this repo's own three-node launcher. If Docker is available,
skip both and run the same three-node topology from the pinned upstream image
CI uses:

```bash
docker run --detach --name endure-localnet \
  --publish 127.0.0.1:9944:9944 \
  --publish 127.0.0.1:9945:9945 \
  --publish 127.0.0.1:9946:9946 \
  ghcr.io/opentensor/subtensor-localnet@sha256:e55f642e34199860f809d7f0c2e8ef660f867ed28b79dde104351b559edfc76a \
  True
```

`True` selects the fast runtime. The image's entrypoint is upstream's
`localnet.sh`, which builds a chainspec and then starts nodes `one`, `two`, and
`three` on RPC ports 9944, 9945, and 9946. Wait for the RPC to answer *and
advance* before seeding — an open socket is not a running chain:

```bash
curl -s -H 'Content-Type: application/json' \
  --data '{"id":1,"jsonrpc":"2.0","method":"chain_getHeader","params":[]}' \
  http://127.0.0.1:9944 | jq -r .result.number
```

Then continue at Phase 3, pointing the seeder and the cycle at node one:

```bash
CHAIN_ENDPOINT=ws://127.0.0.1:9944 bash scripts/dev/seed_chain.sh
make devnet-cycle NETUID=<from seeder> NETWORK=ws://127.0.0.1:9944
```

Chain state lives in the container's writable layer, so `docker rm -f
endure-localnet` is a full reset. Two caveats: the image peers over mDNS
(`--discover-local`) rather than the explicit bootnodes `run_localnet.sh` uses,
so on a host where mDNS is blocked the nodes will sit at `0 peers` — fall back
to Phases 1–2. And container logs are the only chain logs; there is no
`var/chain.log`.

## Phase 1 — Build subtensor

From the repo root, with `subtensor/` cloned as a sibling directory (check
`.gitignore` — it's intentionally not vendored):

```bash
git clone https://github.com/opentensor/subtensor.git   # if not already present
bash subtensor/scripts/localnet.sh --build-only
```

This takes ~7 minutes warm / ~20 minutes cold on M-series Macs. It builds
`subtensor/target/fast-runtime/release/node-subtensor` and writes a
chainspec to `subtensor/scripts/specs/local.json`.

The build produces 4 warnings (`unused_parens`) — upstream noise, safe to
ignore.

## Phase 2 — Launch the three-node chain

**Do not use `subtensor/scripts/localnet.sh` directly to run the chain on
this host.** It relies on libp2p mDNS for peer discovery, which fails on
this macOS with `failed to send mdns query error=IoError(HostUnreachable)`.
The nodes start but stay at `0 peers` / block #0 forever.

Instead, use the Endure-owned launcher which layers explicit bootnodes and
`--no-mdns` over the same binary:

```bash
bash scripts/dev/generate_node_keys.sh     # one-time, generates var/keys/*
tmux new-session -d -s localnet \
  "bash scripts/dev/run_localnet.sh"
```

Check progress:

```bash
tmux attach -t localnet            # live, ctrl-b d to detach
tail -f var/chain.log              # or just the log
```

Within ~10 seconds you should see `💤 Idle (2 peers)` followed by `🏆
Imported #N` at ~1 block/second (fast-runtime). If you see `0 peers` and
mDNS errors, the bootnode wiring is broken — check `var/keys/*.peerid`
matches what the nodes announce in their "Local node identity is: ..." log
line.

## Phase 3 — Seed wallets and chain state

Once the chain is producing blocks, run the idempotent seeder:

```bash
bash scripts/dev/seed_chain.sh
```

It reads `BTCLI` and `PY` (default: the `.venv-seeder` binaries from
`make seeder-install`), `WALLET_PATH` (default: `~/.bittensor/wallets`), and
`CHAIN_ENDPOINT` (default: `ws://127.0.0.1:9946`). To keep the throwaway
wallets inside the checkout the way CI does, seed and run the cycle with the
same path:

```bash
mkdir -p var/wallets
WALLET_PATH=$PWD/var/wallets bash scripts/dev/seed_chain.sh
make devnet-cycle NETUID=<from seeder> NETWORK=ws://127.0.0.1:9946 WALLET_PATH=$PWD/var/wallets
```

It handles all seven on-chain steps in a single command:

1. Create `alice` (from `//Alice` dev URI — pre-funded on localnet),
   `owner`, `miner`, `validator` coldkeys + hotkeys if they don't exist yet
2. Fund `owner`/`miner`/`validator` from `alice` if they have <10 TAO
3. Create the `endure-dev` subnet (or skip if it already exists),
   capturing the assigned netuid — **expect 2, not 1**, because
   netuid 0 is root and earlier slots may be consumed
4. Start the subnet's emission schedule
5. Disable `commit_reveal_weights_enabled` on the subnet (the localnet has no
   drand beacon, so chain-side commit/reveal would strand weight submissions)
6. Register `miner` + `validator` on the subnet
7. Stake TAO from `validator` to itself (without this the validator has
   no permit and cannot set weights)

The script prints the assigned netuid and all key SS58 addresses at the
end. Capture the netuid for Phase 4.

**Re-running is safe.** Every step probes chain/wallet state and skips
work already done. A second invocation fires zero extrinsics.

**Gotchas baked into the seeder** (documented here because they bit us
while writing it, and may bite you if you diverge from the script):

- **MEV protection blocks subnet creation and stake add on localnet.** The
  default MEV shield extrinsic fails with `MEV execution failed: ... wasn't
  decrypted` on the local chain. The seeder passes `--no-mev-protection`
  everywhere. Safe for dev; never disable on mainnet.
- **`SubtokenDisabled(Module)` on stake add.** Subnets need their emission
  schedule started (via `btcli subnets start`) before stake can be added.
  The seeder does this between registration and staking.
- **btcli flag drift.** bittensor-cli 9.x uses hyphenated flags:
  `--wallet.name` → `--wallet-name`,
  `--wallet.hotkey` → `--hotkey`, `--no_prompt` → `--no-prompt`,
  `--no_password` → `--no-use-password`. `--subtensor.chain_endpoint` is
  still accepted in the SDK but the btcli CLI exposes `--network <ws-url>`.
- **`endure/utils/config.py` defaults `--netuid=1`.** You will pass
  the actual assigned netuid (usually 2) to the neurons in Phase 4.

If you prefer to run the steps manually or need to debug a specific
failure, read `scripts/dev/seed_chain.sh` — it's linear and heavily
commented. The equivalent raw `btcli` incantations are derivable from
each step's source block.

## Phase 4 — Launch the Endure neurons

**Use `--subtensor.network ws://127.0.0.1:9946`, not
`--subtensor.chain_endpoint`.** The bittensor SDK silently overrides
`chain_endpoint` back to the finney default if `network` stays at its
default of `finney`. Passing the full `ws://` URL as the `--network` value
sets both fields correctly:

```bash
tmux new-session -d -s endure-miner -c "$(pwd)" \
  ".venv/bin/python neurons/miner.py \
    --netuid 2 \
    --subtensor.network ws://127.0.0.1:9946 \
    --wallet.name miner --wallet.hotkey default \
    --wallet.path $HOME/.bittensor/wallets \
    --endure.active_schema risk.v1.subnet_alpha \
    --logging.debug --axon.port 8091 \
    2>&1 | tee var/miner.log"

tmux new-session -d -s endure-validator -c "$(pwd)" \
  ".venv/bin/python neurons/validator.py \
    --netuid 2 \
    --subtensor.network ws://127.0.0.1:9946 \
    --wallet.name validator --wallet.hotkey default \
    --wallet.path $HOME/.bittensor/wallets \
    --endure.active_schema risk.v1.subnet_alpha \
    --endure.api_port 8714 \
    --logging.debug --axon.port 8092 \
    2>&1 | tee var/validator.log"
```

## Phase 5 — Check process connectivity

Confirm the validator API is live and exposes the Alpha Risk schema:

```bash
curl --fail http://127.0.0.1:8714/health
curl --fail http://127.0.0.1:8714/schemas
grep -F "Miner running" var/miner.log
```

These checks prove local registration, process startup, and the read surface;
they do not wait for the canonical 5-day and 30-day Alpha horizons. Use the
compressed devnet cycle below to exercise commit, reveal, resolution, scoring,
and weight submission in one bounded run.

## Alpha Risk devnet full cycle

This milestone exercises `risk.v1.subnet_alpha` with the compressed devnet
schedule. It still uses the real local
subtensor chain, registered wallets, validator axon, miner dendrite push path,
commit/reveal handlers, scoring DB, and validator weight extrinsic. Bring-up is
unchanged: complete Phases 1–3 above first, keep the chain running, and use the
funded + registered `miner` and `validator` hotkeys printed by
`scripts/dev/seed_chain.sh`.

Run one command from the repo root, replacing `NETUID` with the subnet id from
the seeder (usually `2`):

```bash
make devnet-cycle NETUID=2 NETWORK=ws://127.0.0.1:9946
```

A successful run prints the readiness line and then the checklist:

```text
[x] both neurons ready with <seconds>s epoch runway
[x] round opened
[x] accepted bundles <count>
[x] consensus rows <count>
[x] 5d pass
[x] 30d pass
[x] round closed
[x] miner EMA positive
[x] miner blended score positive
[x] confirmed weight emission batch present
[x] miner confirmed weight non-zero
```

Each invocation writes a hermetic evidence bundle under
`var/devnet-runs/<UTC timestamp>-<pid>/`: its own validator database, miner
state, validator log, and miner log. A run never deletes or reuses another
run's state. For a hotkey that has already served an Axon, the script reuses
its registered port and on-chain IP after proving the local port is free; a
freshly-registered hotkey — the state after Phases 1–3, before any neuron has
run — has no endpoint yet, so the script bootstraps it on a free local port and
lets the neuron auto-detect and serve its own IP on first launch. Either way it
routes the miner to the validator over loopback with
`--endure.validator_axon_overrides`, and requires both neuron loops to reach
their real start markers with at least 15 seconds left before the compressed
epoch. An early child exit fails immediately with the relevant log path.

Runs using the same registered wallets are intentionally serial because an Axon
endpoint belongs to the on-chain hotkey. For parallel cycles, seed separately
registered miner and validator wallets and pass their names and explicit ports;
the runner refuses to collide with an existing local listener.

Weight success is run-scoped: the fresh validator database must contain a
`confirmed` emission batch. A positive weight row left on the chain by an
earlier run does not satisfy the checklist.

The script starts both neurons with:

- `--endure.active_schema risk.v1.subnet_alpha`
- `--endure.devnet_time_compression`
- a unique `--logging.logging_dir`
- `--axon.external_ip` set to each hotkey's already-registered on-chain IP

The runner re-serves each hotkey's registered Axon address rather than a loopback
one — the local chain rejects a loopback `serve_axon` extrinsic, and that failure
is fatal at startup. Miner→validator delivery does not depend on that published
IP: `--endure.validator_axon_overrides` routes the miner to the validator over
`127.0.0.1`, so a changed or unreachable published IP cannot silently strand the
loop.

To exercise persistence with real process faults, run any scenario:

```bash
make devnet-fault-miner NETUID=2 NETWORK=ws://127.0.0.1:9946
make devnet-fault-validator NETUID=2 NETWORK=ws://127.0.0.1:9946
make devnet-fault-miner-state-loss NETUID=2 NETWORK=ws://127.0.0.1:9946
make devnet-faults NETUID=2 NETWORK=ws://127.0.0.1:9946
```

The miner-restart scenario stops it after the validator persists the commit, then
requires the restarted miner to reveal from its durable preimage. The validator
scenario stops it after commit acceptance, reuses the same SQLite database, and
requires the restarted validator to accept the reveal, score, close, emit, and
confirm weights. Fault runs use a longer compressed round so validator startup
cannot consume the reveal window.

The miner-state-loss scenario is the failure counterpart: once the validator has
recorded the commit, it deletes the miner's `risk_miner_state.json` and leaves the
miner stopped — modelling a lost/recreated state volume or a corruption quarantine
from which the committed preimage can never be reproduced. With the nonce gone the
reveal can never be produced, so the commit is stranded: the round still closes,
but the miner scores nothing. The run **passes** only when the checklist names this
outcome — `commit observed; no reveal reached the handler` — turning a
silently-empty round (otherwise diagnosable only a full cycle later on testnet)
into an immediate, explicit signal. In production this is why the miner's
`risk_miner_state.json` must live on a persistent volume: the soak miners mount
`miner-N-state:/root/.bittensor/miners` (`deploy/soak-miners/docker-compose.yaml`)
so a container restart keeps the committed preimage.

The compression guard is hard-coded in startup config: compressed schedules and
any unserved-schema dev override are refused unless the runtime is mock or the
subtensor endpoint resolves to localhost/127.0.0.1. Testnet/mainnet endpoints
such as `test.finney`, `finney`, or archive URLs fail before the neuron serves.
Compression changes only when the 5d/30d passes fire; the realized-value
estimators still evaluate the canonical 5d and 30d fixture block windows, so
recorded fixtures resolve for netuids 8 and 44 while the other whitelist cells
void cleanly.

## Continuous qualification

`.github/workflows/devnet-qualification.yml` runs everything above as one CI
job: it starts the pinned localnet, requires the chain to author two distinct
block heights, installs the seeder's hash-locked toolchain into a throwaway
virtualenv, seeds fresh wallets, and runs `scripts/run_devnet_cycle.py` against
the netuid the seeder reports. The cycle's exit code is the job's verdict.

Triggers match the agreed policy:

| Event | Behavior |
| --- | --- |
| Pull request to `develop` | Manual — add the `qualify` label to the pull request. |
| Push to `develop` | Automatic. |
| Pull request `develop` → `staging` | Automatic. |
| Push to `staging` | Automatic. |

Labelling is the manual trigger rather than `workflow_dispatch` because GitHub
only offers dispatch for workflows present on the default branch, and `main`
trails `develop` by the whole promotion chain. Re-applying the label re-runs the
qualification.

Every run uploads `var/devnet-runs/`, the chain log, and the seeder log as a
`devnet-qualification-<run id>` artifact, including when the run fails — a
failed qualification is only useful if its evidence survives.

This job qualifies Endure's own commit/reveal, scoring, and weight-submission
lifecycle. It is not a deployment rehearsal and not a substitute for the
testnet soak: the localnet has no drand beacon, so `seed_chain.sh` disables
chain-side commit/reveal, and real Subtensor CRv3 weight behavior is still
first observed on staging.

## Tear-down

```bash
docker rm -f endure-localnet          # if you used the container fast path
tmux kill-session -t endure-miner
tmux kill-session -t endure-validator
tmux kill-session -t localnet
pkill -9 node-subtensor
rm -rf var/localnet                    # node state
```

Wallets in `~/.bittensor/wallets/` are safe to keep across runs. Chain
state in `var/localnet/` is ephemeral and should be purged between
fresh-genesis runs.

## Known failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `0 peers` forever, mDNS `HostUnreachable` | macOS networking blocks mDNS | use `scripts/dev/run_localnet.sh` (this repo) instead of upstream |
| `cargo: command not found` | Homebrew rustup doesn't symlink cargo | add `~/.rustup/toolchains/stable-*/bin` to PATH |
| `Error opening genesis file './snapshot.json'` | `node-subtensor key ...` called without `--chain` | pass `--chain subtensor/scripts/specs/local.json` |
| `MEV execution failed: ... wasn't decrypted` on `subnets create` | MEV shield extrinsic fails on localnet | pass `--no-mev-protection` |
| `SubtokenDisabled(Module)` on `stake add` | Subnet emission schedule not started | run `btcli subnets start --netuid N` first |
| Startup summary reports a Finney endpoint | `--subtensor.chain_endpoint` without matching `--subtensor.network` | use `--subtensor.network ws://127.0.0.1:9946` as a single flag |
| `Configured hotkey is not registered on netuid 2` | neuron is pointed at Finney (see above) or wrong netuid | fix network flag or pass the correct `--netuid` |
| Validator log has 0 `set_weights on chain successfully` after 2+ minutes | validator not registered, or no `validator_permit` | check `btcli wallet overview --wallet-name validator` |
| `Ran out of free WASM instances` in chain log | Benign resource warning from substrate under load | ignore |

## What this is not

This setup is a dev loop, not a deployment rehearsal. It intentionally:

- Ships with MEV protection off for subnet creation + staking
- Uses well-known Alice dev keys as a faucet
- Stores wallets with no password
- Runs three nodes on the same host

None of these are acceptable on testnet or mainnet. See
`docs/running_on_testnet.md` and `docs/running_on_mainnet.md` for the
production-oriented paths.
