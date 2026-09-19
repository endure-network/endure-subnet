# Single-host deployment that follows the release channel

This is the supported image-based path for one validator and one miner on a
Linux/amd64 host. The operator fills in one environment file, enables one
timer, and does not touch the host again for ordinary releases: the host
follows the image channel for its stage and upgrades itself, with a database snapshot
before every change and an automatic rollback when the new release is unhealthy.
It does not require a deployment control plane, and Endure never contacts an
operator host.

## How a release reaches the host

The host follows the channel named after its `SERVING_STAGE`; there is no
channel setting.

| `SERVING_STAGE` | Channel | Moved by |
| --- | --- | --- |
| `testnet` | `:testnet` | every promotion to `staging`, once its release checks pass |
| `mainnet` | `:mainnet` (the same image as `:prod`) | a final `vX.Y.Z` release tag |

Promoting a reviewed commit to `staging` publishes validator and miner images
for that exact commit and moves `:testnet` to them, so testnet hosts run what
the Endure staging deployment runs. Those images soak there. Pushing a final
release tag then runs the **Publish prod images** workflow, which retags the
soaked images as `:mainnet`, `:prod` and `:vX.Y.Z` without rebuilding, so the
mainnet channel serves the same bytes that soaked. Within a few minutes each
following host pulls its channel, sees a new image, and deploys it.

Whoever can push to `staging` or push a `v*` tag therefore decides what every
following host runs. The `:mainnet` channel does not exist until the first
final release tag is published; until then a mainnet host has nothing to pull
and `deploy.sh` fails at the pull.

Both GHCR packages must allow unauthenticated pulls before a candidate is
announced publicly. If either package requires a registry credential, treat
that as a release-configuration failure rather than asking public operators to
use an Endure organization token.

## One-time host preparation

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

Replace every example value. These are the values only the operator knows:
wallets, hotkeys, netuid, chain, serving stage, and external IP. Nothing in the
file changes from one release to the next.

Set `VALIDATOR_WALLET_ROOT` and `MINER_WALLET_ROOT` to those separate
directories. The Compose project keeps the existing `endure-subnet` project
name and volume names, so the current validator database and miner nonce state
remain attached. Snapshots are copied outside the Docker volume to
`/var/lib/endure-node/backups`. Never run `docker compose down -v` or prune the
project volumes.

## Preflight

Before a deploy, confirm:

- the wallet directory contains no coldkey secret;
- enough disk space exists for a database snapshot and both images;
- ports 8091 and 8092 are intentionally reachable, while 8714 remains bound to
  localhost unless a separately reviewed TLS proxy is installed.

Render the configuration without starting anything:

```bash
docker compose --env-file deploy/operator-node/.env \
  -f deploy/operator-node/docker-compose.yaml config --quiet
```

## Deploy and follow the channel

Run the first deployment by hand so its output is in front of you (the script
resolves its env file and compose file relative to itself):

```bash
sudo deploy/operator-node/deploy.sh
```

Then enable the update timer. This is the last command the host needs:

```bash
sudo deploy/operator-node/install-timer.sh
```

The installer writes `endure-node-update.service` and
`endure-node-update.timer` pointing at this copy of `deploy.sh`, wherever it
lives, and starts the timer. Every five minutes, and once after a boot that
missed a run, the timer runs the same `deploy.sh`. When the channel has not
moved and `.env` has not changed, that run pulls, compares image IDs, and exits
without a snapshot or a restart. The two channel tags move one after the other,
so a run that lands in between sees a validator and a miner from different
commits; it deploys nothing, exits non-zero, and the next run picks up the
complete release. Follow it with
`journalctl -u endure-node-update.service`.

Editing `.env` later needs no extra step: the next run sees the changed
configuration and recreates the services.

A crashed container is restarted by Docker (`restart: unless-stopped`), not by
the timer, and the timer leaves a stopped node on the current release stopped.
It does start the services when there is something to deploy, so disable the
timer before any maintenance that needs them to stay down
(`sudo systemctl disable --now endure-node-update.timer`) and enable it again
afterwards.

`SERVING_STAGE` must be `testnet` or `mainnet` and must match `CHAIN`: the
neurons refuse to serve Alpha Risk when the acknowledged stage does not match
the configured chain endpoint, and mainnet additionally requires a recognized
mainnet endpoint (see [running_on_mainnet.md](../running_on_mainnet.md)).

When there is something to deploy, the script pulls both images once, snapshots
the live or stopped validator SQLite database with an integrity check and
host-side checksum, records the previous image IDs, and recreates the validator
and miner together from exactly the images it pulled. A deployment is refused
if existing state cannot be backed up. Successful runs capture `/live` and
`/health` results under `/var/lib/endure-node/releases/<timestamp>/`, and
`deployment.txt` there records what was observed: the source revision from the
image label, each image ID, and its registry digest.

If process health fails, the script stops the replacement, restores the
snapshot, and starts the previous validator and miner by their recorded local
image IDs, because the channel tag now resolves to the failed release. It then
lists the failed combination of images and configuration in
`/var/lib/endure-node/releases/rejected-releases.txt` so the timer does not
deploy it again every five minutes. Until something changes, each run exits
non-zero and says so, which leaves the unit visibly failed. A later release
deploys normally, and so does the same release after `.env` is edited, so a
failure caused by a bad `.env` value clears when the value is fixed. To retry
the same release with the same configuration, for example after a health
failure caused by the chain endpoint, remove that file.

A run that fails before it starts anything (a full backup disk, a refused
backup) leaves no release record and no snapshot behind. After a successful
deploy the script removes the images of releases older than the previous one
and keeps the three newest snapshots in `/var/lib/endure-node/backups`. Copy a
snapshot elsewhere if it must outlive that.

A successful script exit proves process health only. The chain-side outcome,
a complete commit/reveal lifecycle and chain-visible weights, shows in
`/health` and on chain, not in the script's exit code.

## Known limit: protocol key changes

Hosts upgrade within one timer interval of each other, not at the same
instant. During a release that changes the protocol key, nodes on different
keys reject each other for up to that interval.

## Rollback and holding a release

A failed release rolls itself back as described above. To go back to an earlier
release by choice, or to hold this host on one release, set `VALIDATOR_IMAGE`
and `MINER_IMAGE` in `.env` to that release's registry digests (the previous
`deployment.txt` records them) and run `deploy.sh`, or wait for the timer. It
takes another pre-change snapshot before switching both services together.
While the pin is set the host does not follow the channel; remove both lines to
resume.

There is one database-boundary exception: release `0014_drop_kre_tables` removes
five legacy KRE tables, and older images know migrations only through `0013`.
Copy the pre-`0014` snapshot out of `/var/lib/endure-node/backups` while it is
still one of the three newest. When rolling back across that boundary, disable
the update timer, stop both services and restore the integrity-checked
pre-`0014` snapshot **before** starting the old images. Do not
run the normal image-swap procedure first, and do not use `alembic downgrade`;
the dropped KRE rows cannot be reconstructed. The automatic rollback after a
failed start or health check already restores the pre-deploy snapshot.

A separate image-identity exception applies to the first migration from legacy
local `:dev` images: those images have no registry digests or source identity.
Preserve the pre-deploy `previous-images.txt`, old local images, and source
directory until the new release completes a full lifecycle. If emergency
rollback to that legacy build is required, disable the update timer, stop, and
use the recorded local image IDs with the previous Compose configuration.

When a rollback requires database restoration, disable the update timer
(`sudo systemctl disable --now endure-node-update.timer`) so a release that
lands mid-restore cannot start the validator on a half-copied file. Then stop
the validator before restoring the pre-deploy snapshot, remove stale SQLite `-wal` and `-shm` files,
start the prior validator and miner images together, then repeat the health,
lifecycle, and chain-side checks. Enable the timer again once the node is
healthy, with the images pinned if the host should stay on the older release.
