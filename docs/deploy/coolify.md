# Endure team Coolify deployment — Alpha Risk testnet

> **Experimental testnet topology only.** Mainnet serving remains code-gated.

This is the Endure team's current staging implementation, not the default
deployment guide for public miners or validators. Public operators should start
with the [testnet runbook](../running_on_testnet.md) or the
[digest-pinned single-host guide](operator-node.md).

The supported soak topology runs the validator and miners as separate Coolify
applications. The validator needs durable database storage and independent
backups; every miner needs durable commit/reveal state. Do not publish endpoint
credentials, wallet contents, backup locations, or host details.

Use the repository's two deployment files:

- `deploy/soak/docker-compose.yaml` — validator and read API.
- `deploy/soak-miners/docker-compose.yaml` — five reference miners.

The root `docker-compose.yml` is a local reference topology, not a supported
public deployment boundary. The files under `deploy/soak/` implement the
team-operated Coolify soak and its documented testnet-only wallet bootstrap.

## Network and ports

- Publish axon ports `8091`–`8096` directly on stable public IP addresses and
  set `EXTERNAL_IP` to the relevant host address. Bittensor advertises an IP and
  port on-chain; do not place axons behind an HTTP reverse proxy.
- Route validator port `8714` through Coolify's private Compose network, TLS,
  and rate limiting. The soak compose file does not publish that port on the
  host, so the proxy is the only external path. The container healthcheck uses
  `/live` for process liveness; monitor `/health` separately for operational
  readiness.
- Run the validator and miners on separate hosts. This avoids local public-IP
  hairpin failures and separates their archive-node request budgets.

## Required configuration

Both applications require:

- `NETUID` — the registered testnet subnet identifier.
- `EXTERNAL_IP` — the public address advertised by that host's axons.
- `WALLETS_TAR_B64` — a base64-encoded, uncompressed tar containing only the
  testnet wallet's `coldkeypub.txt` and required hotkey files.

Optional settings are `CHAIN` (default `test`), `MARKET_DATA_ENDPOINT`,
`EPOCH_LENGTH`, and the validator's `MIN_MINER_STAKE`. A credential-bearing RPC
URL must be stored as a Coolify secret and must use a host accepted by the
runtime serving-stage guard.

The compose files derive the `ENDURE_SOURCE_REVISION` and
`ENDURE_IMAGE_VERSION` build arguments from Coolify's predefined
`SOURCE_COMMIT` variable — the commit Coolify actually checked out — so an
auto-deploy of a new branch tip cannot build new code under a stale label.
`SOURCE_COMMIT` is required. Outside Coolify, export
`SOURCE_COMMIT=$(git rev-parse HEAD)` before building; without it the compose
files render the identity pair `unknown`/`sha-unknown`, which the image's
release-identity check refuses. The former `ENDURE_SOURCE_REVISION` /
`ENDURE_IMAGE_VERSION` manual fallback and the implicit `dev`-image default are
no longer read by these compose files — Coolify's compose parser only supports
single-level `${VAR:-default}` interpolation, so the identity must come from
exactly one variable.

### Testnet wallet exception

`WALLETS_TAR_B64` is a deliberate exception for this team-operated testnet
soak. The one-shot initializer rejects any archive containing a file named
`coldkey`, then extracts the hotkeys into a named volume mounted read-only by
the neurons. Privileged host and container administrators can inspect runtime
environment values, so treat the included hotkeys as testnet-only credentials.

Never use this mechanism for a coldkey, mnemonic, mainnet hotkey, third-party
operator deployment, or public support report. Rotate a hotkey if the Coolify
secret, host, logs, or deployment metadata may have been exposed.

Build the payload on the operator machine, selecting only the testnet wallet
paths required by the deployment:

```bash
cd ~/.bittensor/wallets
tar -cf - <wallet>/coldkeypub.txt <wallet>/hotkeys | base64
```

## Durable state

| Mount | Purpose |
| --- | --- |
| Validator `/data` | SQLite database in the `validator-data` named volume. |
| Validator `/data/backups` | Host-backed SQLite snapshots, independent from the primary volume. |
| Miner `/root/.bittensor/miners` | Nonce and round state required for restart-safe reveals. |
| Neuron `/root/.bittensor/wallets` | Hotkey-only named volume, mounted read-only. |

Disable automatic pruning of unused volumes on every stateful host. A named
volume can become temporarily unreferenced during deployment and is not a
backup boundary. Provision the validator backup directory before deployment:

```bash
sudo install -d -o root -g root -m 0700 /var/lib/endure-soak/backups
```

Schedule a daily SQLite online backup from
`/data/validator-live.db` to `/data/backups`, retain a documented recovery
window, and copy snapshots off-host when possible. Restore drills must stop the
validator, restore a copy without stale `-wal` or `-shm` files, run
`PRAGMA integrity_check`, restart, and verify `/health` plus recent round data.

Do not use `alembic downgrade` for rollback. Apply migrations forward and keep
a restorable pre-deploy database snapshot.

## Runtime behavior

The active schema defaults to `risk.v1.subnet_alpha`. Alpha Risk miners use
`RiskBaselineAssembler` and the configured archive endpoint; do not pass legacy
fixture, strategy, or validator-API options.

`MIN_MINER_STAKE=0` accepts any registered hotkey and is suitable only while
the testnet miners are unstaked. A positive Decimal threshold bounds submission
load but rejects miners below that current metagraph stake. Check chain state
before selecting a value.

The validator read API must expose:

- `/live` for process liveness.
- `/health` for operational readiness.
- `/schemas` for the served schema contract.
- `/risk/v1/subnets` for the signed Alpha Risk feed.

## Deploy and verify

1. Pause automatic deployment for both Coolify applications so a branch update
   cannot restart validator and miners independently.
2. Record both deployment revisions and take a fresh validator snapshot.
3. Render the exact compose configuration without printing secret values.
4. Deploy the validator first; verify its axon advertisement, `/live`,
   `/health`, and `/schemas`.
5. Deploy the miners from the same source revision and preserve their state volumes.
6. Observe one accepted commit/reveal lifecycle and the next confirmed
   chain-side weight emission.
7. For a protocol-key change, redeploy validator and miners together using the
   same image revision.

Coolify compose deployments stop and recreate containers rather than providing
a rolling update. Record the prior deployment identifier before each change.
If rollback is required, restore that revision through the dashboard, restore
the database only when necessary, and repeat the health, round, and weight
checks above.

Migration `0014_drop_kre_tables` is such a necessary case: an `0013` image
cannot start against a database stamped at `0014`. A rollback across this
boundary must stop both applications and restore the verified pre-`0014`
snapshot before the old validator starts. Never use `alembic downgrade`, which
recreates empty legacy table shapes but cannot restore deleted KRE rows.

The protocol key comes from
[`version_contract.py`](../../endure/protocol/version_contract.py), not this
runbook. See the [testnet guide](../running_on_testnet.md) for registration and
direct-process commands.
