# Run a validator or miner from published images

Validators and miners are independent roles. Run only the image for your role;
a miner discovers validators through the subnet metagraph and does not need a
local validator. These Docker examples are a recommended starting point, not a
required infrastructure layout. You choose when to upgrade and whether to
use your own automation.

## Choose an image

Production releases publish separate Linux/amd64 images:

- `ghcr.io/endure-network/endure-subnet-validator:prod`
- `ghcr.io/endure-network/endure-subnet-miner:prod`

`:prod` follows the latest published production release when you pull it.
Use a published `:vX.Y.Z` version tag or `@sha256:<digest>` instead to select a
specific release. A running container does not update when a tag moves.
Until the first final release is published, use qualified testnet candidates;
do not assume that `:prod` is available.

For testnet, select the appropriate role's `:sha-<qualified-staging-commit>`
image or digest from the release artifact. Set `NETUID=504`, `CHAIN=test` and
`SERVING_STAGE=testnet` in the examples below. Mainnet uses netuid `30`,
`CHAIN=finney` and `SERVING_STAGE=mainnet`; see
[the mainnet guide](../running_on_mainnet.md).

## Configure your role

Install Docker and register the hotkey for your chosen role using the
[mainnet](../running_on_mainnet.md#register-and-stake) or
[testnet](../running_on_testnet.md#install-safely) instructions. Prepare an
absolute wallet directory containing only that hotkey and `coldkeypub.txt`,
under `<wallet-name>/hotkeys/<hotkey-name>` and `<wallet-name>/coldkeypub.txt`.
Keep coldkey secrets off the server. No source checkout or local Python
installation is needed to run the images.

Set these values in your shell for either role (replace the example values):

```bash
NETUID=30
CHAIN=finney
SERVING_STAGE=mainnet
WALLET_ROOT=/absolute/path/to/your-role-wallets
WALLET_NAME=replace-me
HOTKEY=replace-me
EXTERNAL_IP=replace-with-your-public-ip
MARKET_DATA_ENDPOINT=wss://archive.chain.opentensor.ai:443
```

## Run a validator

Use a registered validator hotkey with the required chain permit and stake.
The wallet mount is read-only; the named volume retains the validator database.

```bash
VALIDATOR_IMAGE=ghcr.io/endure-network/endure-subnet-validator:prod
docker run -d --name endure-validator --init --stop-timeout 45 --pull always \
  --mount "type=bind,src=$WALLET_ROOT,dst=/root/.bittensor/wallets,readonly" \
  --mount type=volume,src=endure-validator-data,dst=/data \
  -p 8091:8091 -p 127.0.0.1:8714:8714 \
  "$VALIDATOR_IMAGE" \
  --netuid "$NETUID" --subtensor.network "$CHAIN" \
  --wallet.name "$WALLET_NAME" --wallet.hotkey "$HOTKEY" \
  --endure.serving_stage "$SERVING_STAGE" \
  --endure.market_data_endpoint "$MARKET_DATA_ENDPOINT" \
  --endure.database_url sqlite:////data/validator-live.db \
  --endure.api_host 0.0.0.0 --endure.api_port 8714 \
  --axon.port 8091 --axon.external_ip "$EXTERNAL_IP" --logging.info
```

Check `docker logs endure-validator` and
`curl --fail http://127.0.0.1:8714/health`. Publish the axon port as required by
Bittensor; the read API is bound to localhost in this example. See
[validating](../validating.md) for readiness, scoring and weight checks.

## Run a miner

Use your registered miner hotkey. The named volume retains commit/reveal state
so the miner can reveal an outstanding commitment after a restart.

```bash
MINER_IMAGE=ghcr.io/endure-network/endure-subnet-miner:prod
docker run -d --name endure-miner --init --stop-timeout 45 --pull always \
  --mount "type=bind,src=$WALLET_ROOT,dst=/root/.bittensor/wallets,readonly" \
  --mount type=volume,src=endure-miner-state,dst=/root/.bittensor/miners \
  -p 8092:8092 \
  "$MINER_IMAGE" \
  --netuid "$NETUID" --subtensor.network "$CHAIN" \
  --wallet.name "$WALLET_NAME" --wallet.hotkey "$HOTKEY" \
  --endure.serving_stage "$SERVING_STAGE" \
  --endure.market_data_endpoint "$MARKET_DATA_ENDPOINT" \
  --axon.port 8092 --axon.external_ip "$EXTERNAL_IP" --logging.info
```

Check `docker logs endure-miner` for startup and subsequent commit/reveal
activity. See [mining](../mining.md) for eligibility and submission checks.
You do not need a validator wallet or a local validator container.

## Updating your container

When you choose to upgrade, read the release notes for configuration or database
compatibility changes. Back up persistent state as appropriate. Stop and remove
your role's container, then repeat its run command with the same named volume
and wallet mount. `--pull always` fetches the selected image before creating the
container; it does not schedule updates. Do not delete the state volume.
Existing installations should retain their actual volume names, which may
differ from the fresh-install examples above.

## Artifact publication

Promoting a reviewed commit to `staging` automatically runs the **Publish
release images** workflow for that staging tip. The workflow waits for the
required CI jobs for that exact commit, refuses any commit that is no longer
the staging tip, then publishes separate validator and miner images to GHCR
with OCI source labels. It produces a `release-images-<sha>` artifact
containing both digest-pinned references. A manual rerun, once the workflow is
available on the default branch, must provide that same full staging SHA.

This workflow publishes artifacts only. It never contacts an operator host or
changes another deployment environment.

Both GHCR packages must allow unauthenticated pulls before a candidate is
announced publicly. If either package requires a registry credential, treat
that as a release-configuration failure rather than asking public operators to
use an Endure organization token.

## Optional combined validator and miner example

`deploy/operator-node/` is an alternative for operators deliberately running
both roles on one host. Its Compose file and script require both roles and
manage them together; neither is required for the standalone commands above.

### One-time host preparation

Install Docker Engine with the Compose plugin, and Python 3 (the deploy script
runs the host `python3` to parse rendered Compose configuration). Public
candidate images must not require GHCR authentication. Keep the coldkey off the
server. Prepare separate
wallet directories for the validator and miner; each directory must contain
only its own hotkey and the required coldkey public file. Neither service
should be able to read the other service's hotkey.

Copy `deploy/operator-node/` to the host, then create the deployment environment:

```bash
cp deploy/operator-node/env.example deploy/operator-node/.env
chmod 0600 deploy/operator-node/.env
```

Replace every example value. The example uses mainnet `:prod` images; tags or
digests are accepted. For testnet, select both candidate images from one
qualified staging release and change the network settings as described above.
`SOURCE_SHA` is no longer a deployment input. An existing `.env` that still
carries that line keeps working; the line is ignored.

Set `VALIDATOR_WALLET_ROOT` and `MINER_WALLET_ROOT` to those separate
directories. The Compose project keeps the existing `endure-subnet` project
name and volume names, so the current validator database and miner nonce state
remain attached. Snapshots are copied outside the Docker volume to
`/var/lib/endure-node/backups`. Never run `docker compose down -v` or prune the
project volumes.

### Preflight

Before a deploy, confirm:

- the selected release is appropriate for the configured network;
- the current release blocker list is clear;
- the wallet directory contains no coldkey secret;
- enough disk space exists for a database snapshot and both images;
- ports 8091 and 8092 are intentionally reachable, while 8714 remains bound to
  localhost unless a separately reviewed TLS proxy is installed;
- the previous image references and a rollback owner are recorded.

Render the configuration without starting anything:

```bash
docker compose --env-file deploy/operator-node/.env \
  -f deploy/operator-node/docker-compose.yaml config --quiet
```

### Deploy

Run from the directory holding the copied `deploy/operator-node/` (the script
resolves its env file and compose file relative to itself):

```bash
sudo deploy/operator-node/deploy.sh
```

`SERVING_STAGE` must be `testnet` or `mainnet` and must match `CHAIN`: the
neurons refuse to serve Alpha Risk when the acknowledged stage does not match
the configured chain endpoint, and mainnet additionally requires a recognized
mainnet endpoint (see [running_on_mainnet.md](../running_on_mainnet.md)).
Mainnet deployments use releases published on the `:prod` channel by the
release tag workflow, which retags the soaked staging images without rebuilding.

The script snapshots the live or stopped validator SQLite database with an
integrity check and host-side checksum, records the previous image identity,
pulls the selected images, requires matching OCI source revisions and records
the revision, then recreates the validator and miner together without a second
pull. This same-release check applies only to the combined example; independent
operators need protocol-compatible releases, not identical source commits. If
process health fails, it stops the replacement, restores the snapshot, and
starts the prior validator and miner images by their recorded local image IDs. A
deployment is refused if existing state cannot be backed up. Successful runs
capture `/live` and `/health` results under
`/var/lib/endure-node/releases/<timestamp>/`.

A successful script exit proves process health only. For this combined
example, the following evidence helps verify the deployment:

1. exact source SHA and both image digests;
2. backup path and SQLite integrity result;
3. validator and miner protocol keys;
4. one complete commit/reveal lifecycle;
5. scoring evidence and chain-visible weight confirmation;
6. current `/health` status and any degraded fields;
7. the rollback release selected below.

## Rollback

For a normal rollback, replace the two image lines in `.env` with the previous
release's `VALIDATOR_IMAGE` and `MINER_IMAGE` (version tags or digests), then run
`deploy.sh` again. It takes another pre-change snapshot before switching both
services together.

There is one database-boundary exception: release `0014_drop_kre_tables` removes
five legacy KRE tables, and older images know migrations only through `0013`.
When rolling back across that boundary, stop both services and restore the
integrity-checked pre-`0014` snapshot **before** starting the old images. Do not
run the normal image-swap procedure first, and do not use `alembic downgrade`;
the dropped KRE rows cannot be reconstructed. The automatic rollback after a
failed start or health check already restores the pre-deploy snapshot.

A separate image-identity exception applies to the first migration from legacy
local `:dev` images: those images have no registry digests or source identity.
Preserve the pre-deploy `previous-images.txt`, old local images, and source
directory until the new release completes a full lifecycle. If emergency
rollback to that legacy build is required, stop and use the recorded local
image IDs with the previous Compose configuration; the normal script requires
source revision labels that legacy images do not carry.

When a rollback requires database restoration, stop the validator before
restoring the pre-deploy snapshot, remove stale SQLite `-wal` and `-shm` files,
start the prior validator and miner images together, then repeat the health,
lifecycle, and chain-side checks.
