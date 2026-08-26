# Endure Subnet

> **Experimental testnet alpha — `v0.1.0-rc.1` candidate.** Endure is not
> production software, is not economically ready, and must not be used for
> mainnet operation. Alpha Risk serving is code-gated to testnet pending the soak gate.
> The current protocol key is `28` ([contract](endure/protocol/version_contract.py));
> activated and retired leases are tracked in the [version registry](docs/protocol_versions.md).

Endure is a Bittensor risk-intelligence subnet: miners submit falsifiable
assessments, validators resolve and score them, and consumers read signed risk
feeds. The active vertical is **Alpha Risk V1** (`risk.v1.subnet_alpha`):
per-netuid predictions of drawdown, volatility, TWAP price, and liquidity depth.
It does not originate loans, custody collateral, or liquidate positions.

## Contents

- [Alpha Risk incentive loop](#alpha-risk-incentive-loop)
- [Install from source](#install-from-source)
- [Run local mocks](#run-local-mocks)
- [Run a miner](#run-a-miner)
- [Run a validator](#run-a-validator)
- [Register on testnet](#register-on-testnet)
- [Read the API](#read-the-api)
- [Repository history](#repository-history)
- [Contribute and report security issues](#contribute-and-report-security-issues)

## Alpha Risk incentive loop

A miner commits a hash of an assessment bundle, then reveals that exact bundle
and nonce during the reveal window. Validators freeze each round's universe,
resolve observable outcomes, score revealed coordinates, maintain score history,
and emit Bittensor weights. The signed read API publishes the resulting A–E
risk tier. See [mining](docs/mining.md) for the protocol and [the current
scope](docs/specs/2026-07-06-alpha-risk-v1-scope.md) for the product contract.

Known limitations: this is a testnet soak with one public validator endpoint;
outcomes and feeds can diverge between validators; interfaces and
internals may change before a later release. Source builds are supported on the
environment below. Each qualified `staging` commit also publishes candidate
Linux/amd64 validator and miner images; deploy them only by immutable digest
using the [single-host operator guide](docs/deploy/operator-node.md). No stable
semantic-version image exists yet.

Supported source environment: Python `>=3.12,<3.13` and Bittensor `>=10.5,<11`
(see [pyproject.toml](pyproject.toml)). Endure does not perform GPU computation;
representative miner and validator sizing will be published after the testnet
soak produces enough measurements for an honest recommendation.

## Install from source

Use Python 3.12. The checked-in `uv.lock` is authoritative. Bootstrap the pinned
development tools before any locked command:

```bash
git clone https://github.com/endure-network/endure-subnet.git
cd endure-subnet
```

On Debian or Ubuntu, install the venv prerequisite first:

```bash
sudo apt install python3.12-venv
```

```bash
make bootstrap     # installs hash-pinned uv and Gitleaks in the local tool cache
make install       # uv sync --locked --no-dev, creating .venv as needed
# or, for contributors and testnet operators:
make dev-install   # uv sync --locked --extra dev
make verify
```

`make bootstrap` is required even if these tools are already on `PATH`: Endure
uses and verifies the pinned versions from its local user cache. Do not
substitute `pip install` for either Endure install command. The locked
development environment is required before running `make verify`.

## Run local mocks

After `make dev-install`, smoke-test the mock validator and miner with separate
temporary homes. These are independent startup checks, not an end-to-end
network cycle; use [the localnet guide](docs/running_locally.md) for that. The
targets preserve the caller's `HOME`, so runtime state stays in the selected
directory:

```bash
HOME=/tmp/endure-public-validator timeout 90s make dev
# In another shell, within 60 seconds:
curl --fail http://127.0.0.1:8714/health

# Run after the validator exits, using a different home:
HOME=/tmp/endure-public-miner timeout 90s make dev-miner
```

`make dev` serves `/health`; `make dev-miner` logs `Miner running...` once it
reaches steady state. Remove the two temporary homes after the timed runs.

On macOS the `timeout` command is not installed by default; install GNU
coreutils (`brew install coreutils`) and use `gtimeout`, or drop the `timeout`
prefix and stop each process manually.

## Run a miner

Follow the complete public path: [mining guide](docs/mining.md), then the
[testnet runbook](docs/running_on_testnet.md). Never share a mnemonic, coldkey,
hotkey file, seed, wallet archive, or endpoint credential in a public report.

## Run a validator

Follow [validating](docs/validating.md), then the [testnet runbook](docs/running_on_testnet.md).
Validators need durable database storage, backed-up state, a registered testnet
hotkey, and an archive market-data endpoint. Operators who prefer qualified,
digest-pinned images can use the [single-host deployment](docs/deploy/operator-node.md).
Mainnet operation is prohibited.

## Register on testnet

Endure runs on Bittensor testnet netuid `504`. Use a funded testnet wallet and
the documented `btcli` registration commands in
the [testnet runbook](docs/running_on_testnet.md). Registration, stake, permit,
and fee requirements are chain-controlled; check their current values before
launching.

## Read the API

Validators expose process liveness at `/live`, operational readiness at
`/health`, plus `/schemas`, `/rounds`, `/miners`, and
`/risk/v1/subnets`. The current public consumer endpoint is
`https://api.testnet.endure.network`; only one public validator endpoint exists,
so independent-validator quorum is not available. `/schemas` labels each known schema as `served` or
`registered_unserved`; only Alpha Risk is served. A feed signature authenticates
the publishing validator, not the independence or correctness of its market-data
source or a minimum miner quorum. Consumers should inspect `n_submitters` and
apply their own acceptance policy. See the [consumer guide](docs/consuming.md)
and [economic limitations](docs/economic-limitations.md).

## Repository history

This repository intentionally begins with a history-free public snapshot. The
private engineering history was excluded to avoid leaking infrastructure and
operator context; the root snapshot was scanned, signed, and reviewed before
publication. Protocol lineage is independently recorded in the
[activation registry](docs/protocol_versions.md). Public development continues
normally from this snapshot through reviewed pull requests.

## Contribute and report security issues

Read the [documentation index](docs/README.md), [contribution guide](CONTRIBUTING.md),
[code of conduct](CODE_OF_CONDUCT.md), and [security policy](SECURITY.md).
Non-sensitive bug and operator support use the issue forms; security reports go
privately to `hello@endure.network`.

Forge lending remains a documented, dormant vertical: a reference implementation
for future data types and schemas, not a current operator path.
