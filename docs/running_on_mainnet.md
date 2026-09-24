# Running Endure on Mainnet

> **Mainnet serving is opt-in and owner-gated.** `v0.1.0` retains
> experimental economic limitations. Alpha Risk serves on mainnet only when
> the operator passes the explicit `--endure.serving_stage mainnet` acknowledgement on a
> recognized mainnet endpoint, and only releases promoted to the `:prod`
> image channel under the [owner release decision](releases/v0.1.0.md)
> are supported there. Do not run a staging release candidate or a locally built image on mainnet.

The current protocol key is `2041` ([version contract](../endure/protocol/version_contract.py)).
Supported development uses Python `>=3.12,<3.13` and Bittensor `>=10.5,<11`.
Endure's mainnet netuid is `30`; the subnet rates itself under the same rules
as every other member of the Alpha Risk universe.

## The serving-stage acknowledgement

The neurons refuse to serve `risk.v1.subnet_alpha` on any live network
without a stage acknowledgement that matches the configured chain
([`require_serving_stage_allowed`](../endure/utils/config.py)):

| Configured chain | Required flag | Otherwise |
| --- | --- | --- |
| `--subtensor.network finney`, `archive`, or `latent-lite` | `--endure.serving_stage mainnet` | refused |
| `entrypoint-finney.opentensor.ai`, `archive.chain.opentensor.ai`, `lite.sub.latent.to`, or the keyed Dwellir mainnet host as an endpoint or `wss://` network URL | `--endure.serving_stage mainnet` | refused |
| `--subtensor.network test` or a recognized testnet host | `--endure.serving_stage testnet` | refused |
| any other remote endpoint | none accepted | refused |

A testnet acknowledgement on a mainnet endpoint is refused, and so is the
reverse. Self-hosted subtensor nodes on a remote host are not recognized;
extending the allowlist is a code change in `endure/utils/config.py`. Local
chain endpoints (`localhost`, `127.0.0.1`, `::1`) bypass the gate as
development runtimes. `--endure.devnet_time_compression` is refused on
mainnet regardless of the acknowledgement.

## Register and stake

Use funded **mainnet** wallets and the pinned `.venv-seeder/bin/btcli` from
[the testnet runbook's install steps](running_on_testnet.md#install-safely).
Registration, stake, permits, and fees are chain-controlled; check the
prompted values before confirming.

```bash
.venv-seeder/bin/btcli subnets register --netuid 30 --wallet-name <wallet-name> \
  --hotkey <your-role-hotkey> --network finney
.venv-seeder/bin/btcli stake add --netuid 30 --amount <tao> --wallet-name <wallet-name> \
  --hotkey <your-role-hotkey> --network finney
```

Register the hotkey for the role you will run. Validator permits and miner
admission have different requirements; see [validating](validating.md) and
[mining](mining.md).

Keep coldkeys and mnemonics off servers. Mainnet wallet hotkeys are distinct
from testnet hotkeys; a protocol version key is not a wallet key.

## Deploy the `:prod` images

Choose [Run a validator](deploy/operator-node.md#run-a-validator) or
[Run a miner](deploy/operator-node.md#run-a-miner), with `NETUID=30`,
`CHAIN=finney` and `SERVING_STAGE=mainnet`. Each role uses its own image,
wallet and persistent storage; running both is optional.

Use `ghcr.io/endure-network/endure-subnet-validator:prod` for a validator or
`ghcr.io/endure-network/endure-subnet-miner:prod` for a miner. The release-tag
workflow updates `:prod` and publishes `:vX.Y.Z` tags using the published
staging candidate digests without rebuilding. A published version tag or
digest can be selected instead.
Merging into `main` alone does not publish a production release.

You choose when to pull and recreate your container, or automate that in your
own infrastructure. See the [container guide](deploy/operator-node.md#updating-your-container)
for persistent-state considerations. The optional two-service example also
accepts tags and digests.

## Weights and abstention

A validator sets weights only when at least one active coordinate has a
positive score; otherwise it abstains and leaves previously submitted on-chain
weights untouched ([validating.md](validating.md)). Resolution runs against
the configured `MARKET_DATA_ENDPOINT`, which defaults to the mainnet archive
node.

Untouched weights still expire. Once a validator's last weight update is older
than the subnet's `activity_cutoff` hyperparameter (5000 blocks, about 16.7
hours, on SN30 at the time of writing), Yuma consensus stops counting its
stake and its dividends decay. A validator's first positive score arrives five
days after the reveal window of the first round in which it accepts a
submission, so a new Endure validator abstains for at least that long, and
indefinitely while no miner submits to it. Verify `activity_cutoff` with
`btcli` before relying on these numbers.

Until Endure announces the mainnet weight cutover, keep your existing SN30
weight setter running on the validator hotkey and add
`--neuron.disable_set_weights` to the Endure validator command, so exactly one
process writes weights for that hotkey. Two weight writers on one hotkey
overwrite each other. Remove the flag and stop the other setter only at the
cutover.
