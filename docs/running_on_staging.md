# Running Endure on Staging

`staging` is both the release-candidate branch and the Endure-operated testnet
environment. It is a promotion and verification boundary, not a deployment
provider and not a claim of mainnet readiness.

Changes reach staging only through a merge-commit promotion from `develop`.
The promotion must pass the repository gates and localnet qualification. After
merge, the staging tip passes CI and publishes source-bound, digest-addressed
validator and miner images. The deployed environment must preserve validator
and miner state and evidence its exact source revision through health,
commit/reveal, scoring, and confirmed-weight evidence.

`/health` reports two identity fields, and they carry different weight.
`source_revision` (with its `image_version`) is an operator attestation: a build
argument baked into the image from the commit the builder checked out. A
consumer cannot independently verify it. `content_revision` is computed at
runtime from the running package sources, so anyone can recompute it from a
checkout of that commit and compare:

```bash
python -m scripts.content_revision
```

A mismatch means the deployment is not running that source, whatever the
attested revision says.

Public operators do not need to run the `staging` branch or use the Endure
team's hosting provider. Use the [testnet runbook](running_on_testnet.md) for
direct source operation or the
[single-host operator guide](deploy/operator-node.md) for qualified immutable
images. The Endure team's current Coolify implementation remains a separate
[maintainer runbook](deploy/coolify.md).

## Testnet soak gate

The soak gate is the promotion evidence the mainnet decision consumes. It
passes when the deployed staging environment shows, over seven consecutive
days with an unchanged deployed revision:

- zero unexplained validator restarts (planned redeploys reset the window);
- zero `overdue_rounds` in `/health`;
- scheduled soak probes green outside a ten-minute post-restart grace window;
- confirmed weight emissions on every day where positive scores existed.

Any redeploy, revision change, or unexplained restart resets the seven-day
clock. The gate is evaluated against probe history and persisted `/health`
evidence, not operator recollection.

Mainnet requires a separate promotion decision after the testnet soak gate
passes: a final release tag publishes the soaked images to the `:prod` channel,
and serving there needs the explicit acknowledgement described in
[running_on_mainnet.md](running_on_mainnet.md).
