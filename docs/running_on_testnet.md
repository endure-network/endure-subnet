# Running Endure on Testnet

> **Experimental testnet alpha, unreleased `v0.1.0` candidate, protocol key `27`.** This is
> not a mainnet guide. The authoritative compatibility value is
> [version_contract.py](../endure/protocol/version_contract.py).

## Install safely

```bash
git clone https://github.com/Endure-Network/endure-subnet.git
cd endure-subnet
make bootstrap
make dev-install
make verify
```

`make dev-install` runs `uv sync --locked --extra dev` against the checked-in
lockfile. `make bootstrap` installs the pinned uv and Gitleaks tools into a
local user cache, which every Endure locked target verifies before its command.
Do not replace `make bootstrap` or `make dev-install` with `pip install`.

Use only funded **testnet** wallets. Create/register the validator and miner
hotkeys with your installed `btcli`; registration, stake, permits, fees, and
the testnet endpoint are chain-controlled and must be checked at execution.
Keep coldkeys and mnemonics off servers. Provision only the required testnet
hotkey plus `coldkeypub.txt` through the documented operator path, and never
copy wallet material into an issue or log.

For the current command shapes, substitute the actual testnet netuid and wallet
names, then check the prompted fee and chain state before confirming:

```bash
btcli subnet register --netuid <netuid> --wallet.name <wallet-name> \
  --wallet.hotkey <validator-hotkey> --subtensor.network test
btcli stake add --netuid <netuid> --amount <tao> --wallet.name <wallet-name> \
  --wallet.hotkey <validator-hotkey> --subtensor.network test
```

Register and stake the miner hotkey the same way. `btcli` changes independently
of Endure; use its installed `--help` output if these option names differ.

## Validator first

Start one validator with a registered hotkey, persistent database URL, a
reachable axon address, and `--endure.serving_stage testnet`. Set
`--endure.market_data_endpoint` to your archive source. The command is:

```bash
.venv/bin/python neurons/validator.py --netuid <netuid> --subtensor.network test \
  --wallet.name <wallet-name> --wallet.hotkey <validator-hotkey> \
  --endure.serving_stage testnet --endure.database_url <persistent-db-url> \
  --endure.market_data_endpoint <archive-endpoint> --endure.api_port 8714 \
  --axon.port <axon-port> --axon.external_ip <reachable-ip>
```

Expose the axon as Bittensor requires; expose the read API separately behind
appropriate TLS and rate limits. Confirm `/health` and `/schemas` before
starting miners; `/live` is process liveness only and must not replace the
operational `/health` check. Back up and restore-test the persistent database; restart
behavior depends on retained durable state. See [validating](validating.md).

## Then start miners

```bash
.venv/bin/python neurons/miner.py --netuid <netuid> --subtensor.network test \
  --wallet.name <wallet-name> --wallet.hotkey <miner-hotkey> \
  --endure.serving_stage testnet --endure.market_data_endpoint <archive-endpoint> \
  --axon.port <axon-port> --axon.external_ip <reachable-ip>
```

Wait for metagraph/permit discovery, then verify a commit and a reveal in the
validator API/logs. Preserve miner state across restarts so its nonce survives.
See [mining](mining.md) for the commit/reveal contract and troubleshooting.

## Deployment topology

Use the direct commands above or the
[digest-pinned single-host deployment](deploy/operator-node.md). Neither public
path requires Coolify. The root `docker-compose.yml` remains a local reference
topology; it cannot enforce separation between host wallet paths and is not a
supported public deployment boundary. The Endure team operates its own
multi-host soak through Coolify; that provider-specific procedure is documented
separately in [the maintainer runbook](deploy/coolify.md).

The [`staging` contract](running_on_staging.md) describes the release-candidate
branch and environment independently of any deployment provider.

Mainnet serving remains prohibited until the soak gate and a later code change.

Compressed synthetic rounds are now reached through
`--endure.devnet_time_compression` and `--endure.synthetic_epoch`; there is no
fixture path to replace.
