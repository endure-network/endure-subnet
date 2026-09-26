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

For the initial `v0.1.0` publication, the owner accepted the accumulated
staging run on 2026-09-20 and did not require a fresh seven-day window for
the deployment/documentation and package-version changes. Staging continues
to run. See the [release decision](releases/v0.1.0.md); this is a scoped
exception, not evidence that an unchanged seven-day window completed.

For protocol key `2042` (`v0.1.1`), the owner decided on 2026-09-24 not to wait
for a fresh seven-day window. Validators on key `2041` abstain until they have
scores, so with no miners yet their weights age past `activity_cutoff`; `2042`
replaces that abstention with the owner vote whenever no miner has a positive
score. On 2026-09-26 the owner scoped this second exception to the
unchanged-revision rule: a targeted soak of roughly 24 hours on the exact
release commit, with a confirmed key-`2042` testnet weight submission, one full
round cycle and a restart as checkpoints, followed by a mainnet canary on the
team's own validator (SN30 UID 195) before third-party operators. CI and
localnet qualification remain required for the selected staging commit. The
[v0.1.1 release notes](releases/v0.1.1.md) record the decision, and the GitHub
release records the checkpoint outcomes.

The default soak gate is the promotion evidence the mainnet decision consumes.
It passes when the deployed staging environment shows, over seven consecutive
days with an unchanged deployed revision:

- zero unexplained validator restarts (planned redeploys reset the window);
- zero `overdue_rounds` in `/health`;
- scheduled soak probes green outside a ten-minute post-restart grace window;
- confirmed weight emissions on every day where positive scores existed.

Any redeploy, revision change, or unexplained restart resets the seven-day
clock. The gate is evaluated against probe history and persisted `/health`
evidence, not operator recollection.

Mainnet requires a separate promotion decision after the testnet soak gate
passes or an explicit release exception is recorded: a final release tag
publishes the staging candidate images to the `:prod` channel,
and serving there needs the explicit acknowledgement described in
[running_on_mainnet.md](running_on_mainnet.md).
