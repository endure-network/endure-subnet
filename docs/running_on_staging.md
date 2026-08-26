# Running Endure on Staging

`staging` is both the release-candidate branch and the Endure-operated testnet
environment. It is a promotion and verification boundary, not a deployment
provider and not a claim of mainnet readiness.

Changes reach staging only through a merge-commit promotion from `develop`.
The promotion must pass the repository gates and localnet qualification. After
merge, the staging tip passes CI and publishes source-bound, digest-addressed
validator and miner images. The deployed environment must preserve validator
and miner state and prove its exact source revision through health,
commit/reveal, scoring, and confirmed-weight evidence.

Public operators do not need to run the `staging` branch or use the Endure
team's hosting provider. Use the [testnet runbook](running_on_testnet.md) for
direct source operation or the
[single-host operator guide](deploy/operator-node.md) for qualified immutable
images. The Endure team's current Coolify implementation remains a separate
[maintainer runbook](deploy/coolify.md).

Mainnet remains code-gated and requires a separate promotion decision after the
testnet soak gate passes.
