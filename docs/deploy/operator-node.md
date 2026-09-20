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

Channel publication is a separate job that starts only after the digest
artifact is saved. If one tag update fails, use **Re-run failed jobs** to retry
that job from the same artifact without rebuilding. Existing hosts keep their
previous release while the pair is split. Re-running the publication job also
reuses existing `sha-*` images rather than overwriting them.

Whoever can push to `staging` or push a `v*` tag therefore decides what every
following host runs. The `:mainnet` channel does not exist until the first
final release tag is published; until then a mainnet host has nothing to pull
and `deploy.sh` fails at the pull.

Both GHCR packages must allow unauthenticated pulls before a candidate is
announced publicly. If either package requires a registry credential, treat
that as a release-configuration failure rather than asking public operators to
use an Endure organization token.

## One-time host preparation

Install Docker Engine with the Compose plugin, Python 3, Bash 4 or newer,
`flock`, `realpath`, `sha256sum` and `curl` (the deploy script
runs the host `python3` to parse rendered Compose configuration). Public
candidate images must not require GHCR authentication. Keep the coldkey off the
server. Prepare separate
wallet directories for the validator and miner; each directory must contain
only its own hotkey and the required coldkey public file. Neither service
should be able to read the other service's hotkey.

Install the operator files in `/opt/endure-node`, owned by root and not writable
by other accounts. The optional system timer runs from this directory, not a
working checkout. From the checkout root, for a **new installation**:

```bash
sudo install -d -o root -g root -m 0755 /opt/endure-node
sudo install -o root -g root -m 0755 deploy/operator-node/deploy.sh deploy/operator-node/install-timer.sh /opt/endure-node/
sudo install -o root -g root -m 0644 deploy/operator-node/docker-compose.yaml deploy/operator-node/endure-node-update.service deploy/operator-node/endure-node-update.timer /opt/endure-node/
sudo install -o root -g root -m 0600 deploy/operator-node/env.example /opt/endure-node/.env
```

Edit `/opt/endure-node/.env` using `sudoedit`. The installer rejects symlinks,
non-root ownership, and group/other-writable runtime files or ancestor directories.
For an existing host, preserve its `.env` instead of installing `env.example`.
Disable any old update timer and wait on the deployment lock before replacing
installed files; keep the Compose project and volume names unchanged.

### Migrating an existing rc.3 configuration

Copy the existing `.env` into `/opt/endure-node/.env` with root ownership and mode
0600. Existing `VALIDATOR_IMAGE` and `MINER_IMAGE` values remain explicit pins.
To opt into channel updates, remove **both** lines after confirming the desired
`SERVING_STAGE`; remove the obsolete `SOURCE_SHA` line too. Keep both digest
lines to stay pinned. Every deploy reports each service's channel or explicit
image override, and enabling the timer does not remove pins.

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
sudo docker compose --env-file /opt/endure-node/.env \
  -f /opt/endure-node/docker-compose.yaml config --quiet
```

## Deploy and follow the channel

Run the first deployment by hand so its output is in front of you (the script
resolves its env file and compose file relative to itself):

```bash
sudo /opt/endure-node/deploy.sh
```

Then enable the update timer. This is the last command the host needs:

```bash
sudo /opt/endure-node/install-timer.sh
```

The installer writes `endure-node-update.service` and
`endure-node-update.timer` pointing at `/opt/endure-node/deploy.sh`, and starts the timer. Every five minutes, and once after a boot that
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
It does start the services when there is something to deploy. Before maintenance
that needs them to stay down, follow [the maintenance procedure](#maintenance-and-database-restoration)
to disable the timer and exclude any deployment already running.

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
deploys normally after a successful rollback, and a changed `.env` creates a
new identity. If rollback also failed, use the maintenance recovery procedure
before retrying. Successful rollback to the same identity clears its rejection.
To retry
the same release with the same configuration, for example after a health
failure caused by the chain endpoint, remove that file.

A backup failure leaves services unchanged, removes partial host snapshots and
attempts removal of the temporary in-volume copy. Backup attempts are limited
to once per hour after failure; remove `backup-retry-after` only if instructed
by the error message after correcting the problem. A killed process may leave
one temporary in-volume copy, which the next attempt overwrites.

On first adoption, the deployer protects all existing recorded rollback images
and snapshots plus the immediate pre-upgrade baseline. These are listed in
`/var/lib/endure-node/releases/protected-images.txt` and `protected-backups.txt`.
They remain protected until an operator explicitly removes their entries.
Before a later migration needing a longer rollback window, add its recovery
image IDs and snapshot paths to those lists under the deployment lock.
Other managed images retain the running and previous release; other snapshots
retain the newest three. Failed-release images join the cleanup inventory.
Completed managed records retain the newest twenty; older unmanaged records
and the adoption record are preserved. Image removal never forces deletion of
images still in use.

If a deployment is interrupted, `pending-deployment` points to its recovery
record (target identity, previous images and snapshot checksum). Later runs
refuse deployment until an operator enters maintenance and either verifies the
attempted release or restores the recorded baseline. Matching containers alone
never clear this marker. A failed automatic rollback also leaves it in place.

A successful script exit proves process health only. The chain-side outcome,
a complete commit/reveal lifecycle and chain-visible weights, shows in
`/health` and on chain, not in the script's exit code.

## Known limit: protocol key changes

Healthy hosts normally pick up a release within a few minutes, including timer
jitter, pull and startup time. During a release that changes the protocol key,
nodes on different keys reject each other. A pinned host or a failed update can
extend that mismatch until the operator intervenes.

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
Keep the pre-`0014` snapshot and its matching images in the protected recovery
lists described above (existing snapshots are protected on first adoption). When rolling back across that boundary, enter
[maintenance](#maintenance-and-database-restoration), stop both services and restore the integrity-checked
pre-`0014` snapshot **before** starting the old images. Do not
run the normal image-swap procedure first, and do not use `alembic downgrade`;
the dropped KRE rows cannot be reconstructed. The automatic rollback after a
failed start or health check already restores the pre-deploy snapshot.

A separate image-identity exception applies to the first migration from legacy
local `:dev` images: those images have no registry digests or source identity.
Preserve the pre-deploy `previous-images.txt`, old local images, and source
directory until the new release completes a full lifecycle. If emergency
rollback to that legacy build is required, enter
[maintenance](#maintenance-and-database-restoration), stop, and use the recorded
local image IDs with the previous Compose configuration.

## Maintenance and database restoration

Disabling the timer prevents future runs; it does not stop an update already
running. From the checkout root, open a root shell and acquire the same lock
used by `deploy.sh` before stopping services or touching their state:

```bash
sudo systemctl disable --now endure-node-update.timer
sudo bash
exec 9>/var/lib/endure-node/releases/deploy.lock
flock -x 9
```

Wait for `flock` to return. It waits for any deployment holding the lock to
finish; keep this shell open to hold the lock throughout maintenance. Do not
kill an update midway through its backup or rollback.

In this shell, stop both services. Restore the integrity-checked snapshot and
remove stale SQLite `-wal` and `-shm` files while the validator is stopped.
Pin both images to the intended registry digests in `/opt/endure-node/.env`.
For this maintenance restart, resolve those pins to local image IDs just as the
deployer does, so its next run sees the same Compose configuration hash:

```bash
compose=(docker compose --env-file /opt/endure-node/.env -f /opt/endure-node/docker-compose.yaml)
"${compose[@]}" pull validator miner-1
mapfile -t refs < <("${compose[@]}" config --format json | python3 -c 'import json,sys; s=json.load(sys.stdin)["services"]; print(s["validator"]["image"]); print(s["miner-1"]["image"])')
export VALIDATOR_IMAGE="$(docker image inspect --format '{{.Id}}' "${refs[0]}")"
export MINER_IMAGE="$(docker image inspect --format '{{.Id}}' "${refs[1]}")"
"${compose[@]}" up -d --no-build --pull never validator miner-1
```

Keep the digest pins in `.env`; these environment overrides last only for this
shell. Direct Compose is reserved for this locked restoration procedure;
routine installs and upgrades use `deploy.sh` for its backup and health gates.
Do not call `deploy.sh` while holding its lock: it will refuse to run. Repeat
the health, lifecycle and chain-side checks. If recovering an interrupted or
rejected attempt, only after successful verification remove `pending-deployment`
and `rejected-releases.txt` from `/var/lib/endure-node/releases`; retain their
referenced recovery records. Leave this shell with `exit`, releasing the lock,
then run `sudo /opt/endure-node/deploy.sh` and confirm it reports no change
before re-enabling the timer.

Once maintenance is complete and the node is healthy, re-enable updates:

```bash
sudo systemctl enable --now endure-node-update.timer
```

Keep the image pins if the host should stay on the restored release.
