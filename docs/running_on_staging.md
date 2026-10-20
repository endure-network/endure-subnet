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

For protocol key `2043` (`v0.1.2`), the owner accepted the existing testnet
run at staging commit `02ab118` on 2026-10-05 and waived an additional timed
soak for the documentation and version-metadata release. The protocol code
is unchanged. Successful CI, localnet qualification and published images on
the exact final staging commit remain required, along with verification of
the deployed version, source/content identity, validator loop and miners.
The known commit-reveal startup interval is monitored separately; publication
does not require a new confirmed vote or a fresh full round. Before the
mainnet cutover, publish the intended SN30 burn rate and use UID 195 as the
canary. The [v0.1.2 release notes](releases/v0.1.2.md) record this scoped
exception and the limitations of the earlier soak evidence; the GitHub
release records the actual release-check outcomes.

For protocol key `2044` (`v0.1.3`), the owner decided on 2026-10-16 not to wait
for a seven-day window. The scoped exception has three parts. First, private
qualification of the release's source tree before it is public: `make verify`,
and a local devnet run in which two miners revealing identical values share one
miner's on-chain incentive, with a restart that rebuilds the pools. The staging
commit that the tag promotes must carry the same tree, or the devnet run is
repeated on it. Then a targeted soak of at most about 24 hours on testnet, with
a confirmed key-`2044` weight submission and a deliberate restart after it that
comes back to another confirmed submission as checkpoints, and, added on
2026-10-19, one full round: the first testnet round run on key `2044` from
commit to reveal close joins the pool window, and a confirmed submission is
computed from it. Then the `:prod` promotion between 00:00 and 11:00 UTC,
before the day's commit window opens. Validators that track `:prod` may upgrade
on promotion; the team's own validator cuts over first as the mainnet canary,
and operators are asked to upgrade once it has a confirmed key-`2044` weight
submission and `/miners` reports `pool_size`. CI, localnet qualification and
published images on the exact final staging commit remain required. The
[v0.1.3 release notes](releases/v0.1.3.md) record the decision, and the GitHub
release records the checkpoint and canary outcomes.

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
