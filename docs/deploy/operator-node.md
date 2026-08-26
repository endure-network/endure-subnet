# Single-host testnet deployment with immutable images

This is the supported image-based testnet path for one validator and one miner
on a Linux/amd64 host. It does not require a deployment control plane and does
not auto-deploy. The operator selects a qualified staging commit, takes a
backup, and starts both services from registry digests that identify exact
image bytes. Until `v0.1.0` is tagged, these are release-candidate images rather
than stable semantic-version releases.

Do not run this procedure while a release blocker is open. Mainnet deployment
also requires the documented soak and release approvals; publishing an image
does not authorize deploying it.

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

Replace every example value. Copy `SOURCE_SHA`, `VALIDATOR_IMAGE`, and
`MINER_IMAGE` exactly from the workflow artifact. Both image references must
end in `@sha256:<64 lowercase hexadecimal characters>`.

Set `VALIDATOR_WALLET_ROOT` and `MINER_WALLET_ROOT` to those separate
directories. The Compose project keeps the existing `endure-subnet` project
name and volume names, so the current validator database and miner nonce state
remain attached. Snapshots are copied outside the Docker volume to
`/var/lib/endure-node/backups`. Never run `docker compose down -v` or prune the
project volumes.

## Preflight

Before a deploy, confirm:

- the selected SHA is the approved staging candidate;
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

## Deploy

Run from the directory holding the copied `deploy/operator-node/` (the script
resolves its env file and compose file relative to itself):

```bash
sudo deploy/operator-node/deploy.sh
```

The current script refuses `SERVING_STAGE=mainnet`. Enabling mainnet is a
separate post-soak repository change after the canonical mainnet gate and
release approvals are complete.

The script refuses mutable image tags, requires both OCI revisions to match
`SOURCE_SHA`, snapshots the live or stopped validator SQLite database with an
integrity check and host-side checksum, records the previous image identity,
pulls the two digests, and recreates the validator and miner together. If
process health fails, it stops the replacement, restores the snapshot, and
starts the prior validator and miner images without pulling mutable tags. A
deployment is refused if existing state cannot be backed up. Successful runs
capture `/live` and `/health` results under
`/var/lib/endure-node/releases/<timestamp>/`.

A successful script exit proves process health only. Before accepting the
deployment, record all of the following in the private operations board:

1. exact source SHA and both image digests;
2. backup path and SQLite integrity result;
3. validator and miner protocol keys;
4. one complete commit/reveal lifecycle;
5. scoring evidence and chain-visible weight confirmation;
6. current `/health` status and any degraded fields;
7. the rollback release selected below.

## Rollback

For a normal rollback, replace the three artifact lines in `.env` with the
previous release's `SOURCE_SHA`, `VALIDATOR_IMAGE`, and `MINER_IMAGE`, then run
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
image IDs with the previous Compose configuration; do not weaken the immutable
image check in `deploy.sh`.

When a rollback requires database restoration, stop the validator before
restoring the pre-deploy snapshot, remove stale SQLite `-wal` and `-shm` files,
start the prior validator and miner images together, then repeat the health,
lifecycle, and chain-side checks.
