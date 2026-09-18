# Running Endure on Mainnet

> **Mainnet serving is opt-in and owner-gated.** `v0.1.0-rc.3` is an
> experimental alpha. Alpha Risk serves on mainnet only when the operator
> passes the explicit `--endure.serving_stage mainnet` acknowledgement on a
> recognized mainnet endpoint, and only releases promoted to the `:prod`
> image channel after the testnet soak gate are supported there. Do not run
> a staging release candidate or a locally built image on mainnet.

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
  --hotkey <validator-hotkey> --network finney
.venv-seeder/bin/btcli stake add --netuid 30 --amount <tao> --wallet-name <wallet-name> \
  --hotkey <validator-hotkey> --network finney
```

Keep coldkeys and mnemonics off servers. Mainnet wallet hotkeys are distinct
from testnet hotkeys; a protocol version key is not a wallet key.

## Deploy the `:prod` images

Third-party validators deploy through the
[operator node path](deploy/operator-node.md) with `CHAIN=finney` and
`SERVING_STAGE=mainnet`. There is no image to choose: the host follows
`ghcr.io/endure-network/endure-subnet-validator:prod` and
`ghcr.io/endure-network/endure-subnet-miner:prod` by default and upgrades itself
when a release moves the channel. The `:prod` channel is written only by the
release-tag workflow, which retags the images that soaked on staging without
rebuilding, so the published digests equal the soaked ones.

Rollback, and pinning a host to one release, are covered in
[operator-node.md](deploy/operator-node.md#rollback-and-holding-a-release).

## Weights and abstention

A validator sets weights only when at least one active coordinate has a
positive score; otherwise it abstains and leaves previously submitted on-chain
weights untouched ([validating.md](validating.md)). Resolution runs against
the configured `MARKET_DATA_ENDPOINT`, which defaults to the mainnet archive
node.
