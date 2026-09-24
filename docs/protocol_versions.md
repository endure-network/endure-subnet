# Protocol activation history

The machine-readable registry at
[`endure/protocol/activated_versions.json`](../endure/protocol/activated_versions.json)
separates retired activation history from the single unactivated current lease.
An activation is the first appearance of a distinct current protocol key and
digest pair on the first-parent staging lineage. Historical keys may repeat or
skip because each distinct activated assignment is recorded chronologically.

Public activation evidence and lease authority are lowercase SHA-256 receipts.
Historical private preimages remain in the release evidence ledger; the clean
public root and every post-public lease publish reproducible authority preimages.
The protocol contract
pins both the canonical activation-history digest and the complete registry
byte digest.
The protocol-version guard also reconstructs first appearances from the
first-parent `origin/staging` history and verifies every public receipt against
its introducing commit.

The public repository intentionally starts from one clean snapshot at key `27`.
That immutable key/digest pair is the public-history trust root: its new root
commit cannot reproduce the private introducing-commit receipt, so only that
one receipt is exempt. Every later public activation remains source-bound and
must match the available first-parent staging suffix exactly.

The lineage comparison permits exactly two release states:

- before promotion, the staging lineage exactly equals the recorded activation
  history;
- immediately after promotion, the lineage may equal that history plus one tail
  assignment whose key and digest exactly match the exclusive current lease.

No other unrecorded lineage suffix is valid, apart from the explicit key-`27`
public-history root described above. The second state keeps staging CI
green after a merge-commit promotion, whose source commit ID cannot be known in
advance, without treating the new assignment as retired history prematurely.

Each historical receipt is SHA-256 over this UTF-8 private preimage:

```text
SOURCE_COMMIT_SHA1=<full lowercase introducing commit object ID>
CURRENT_VERSION_KEY=<decimal protocol key>
CURRENT_VERSION_DIGEST=<lowercase 64-hex digest>
```

Every line ends with one LF byte, including the final digest line. There is no
BOM, blank line, or other whitespace. Only the resulting receipt is public;
the substituted source value and its mapping stay in the private evidence
ledger.

Run `python -m scripts.quality_gates.checks activation-digests` to print the
canonical candidate values after an authorized registry update. The command is
read-only; copy its output into the protocol contract only after reviewing the
staging-lineage comparison.

After a leased assignment first reaches staging, use the promotion merge commit
ID to calculate its source-bound receipt. In the next candidate update, append
that assignment to `activation_history`, advance `previous_activation_id`, issue
a unique higher current lease, and update the contract's previous-assignment and
registry digest constants. The new lease can then follow the same promotion
cycle. Never append the leased tail before its staging merge commit exists.

Protocol key `27` is the clean public-history trust root and is recorded as
`activation-0040` with digest
`d0884ffa6bf8d98807d20ab9ee8a7a0c2821bb08d0cc6376fb87a6db605cf0fb`.
Its public root commit has a synthetic identity rather than the private staging
promotion identity, so the lineage guard exempts only that root receipt while
still requiring its key and digest exactly.

Key `28` is recorded as `activation-0041`. It carries the bounded live-sampling
fix, missing-timestamp settlement, round-aware miner eligibility, and the
next-commit-close publication embargo. Its watched-tree digest is
`05da1df37dc67de435d0954d9b102be45922c6956822643ff1dcc7a892176e26`.
It first appeared on the public first-parent staging lineage in commit
`d56bcc7fac2f4966568526446f1491156da4c753`; applying the source-bound receipt
format above produces
`be858c97c085aba909bcb0784b09e268474e32991444404435970c8f64241aed`.

The original key-`28` lease authority was also publicly reproducible: SHA-256
over the UTF-8 lines `LEASE_AUTHORITY`,
`PREVIOUS_RECEIPT=cf3226d57dc49d5f84fed5d8eb79676c2fd215c082de214202b9a368571be5e9`,
`CURRENT_VERSION_KEY=28`, and
`CURRENT_VERSION_DIGEST=05da1df37dc67de435d0954d9b102be45922c6956822643ff1dcc7a892176e26`,
each terminated by one LF byte, produces
`fa41045b844d60c22340a5ed0fd8118cc53c83031490eea755c2cdb05c9ccd71`.

Key `29` was leased exclusively to the final `v0.1.0-rc.1` candidate. It
removes stale source citations and an obsolete reference-miner roadmap
promise, and consensus publication now skips an accepted bundle that no longer
parses — the policy scoring already applied — instead of leaving the round
open. Wire formats, scoring math, and the aggregation of parseable bundles are
unchanged. Its watched-tree digest is
`d3b9126c2bad0045e497e6f5f7362309c004d340f927cc91638d4df84344379b`.
Its public lease authority receipt is SHA-256 over the UTF-8 lines
`LEASE_AUTHORITY`,
`PREVIOUS_RECEIPT=fa41045b844d60c22340a5ed0fd8118cc53c83031490eea755c2cdb05c9ccd71`,
`CURRENT_VERSION_KEY=29`, and
`CURRENT_VERSION_DIGEST=d3b9126c2bad0045e497e6f5f7362309c004d340f927cc91638d4df84344379b`,
each terminated by one LF byte. The resulting receipt is
`c4aa1b087b26039b30524093943467ae5074939aaf35c43c87fcb79ffc66ae13`.

Key `29` is recorded as `activation-0042`. It first appeared on the public
first-parent staging lineage in commit
`c072b9a6dd65327daca85fa152099cc392414cbe`; applying the source-bound receipt
format above produces
`d8bd3956158777b7f4355e9298abe2a7d411ef42914b3d5520fdd4bc0edc5f71`.

Key `30` was leased exclusively to the `v0.1.0-rc.2`/`v0.1.0-rc.3` candidate
line — `v0.1.0-rc.3` changes no watched protocol path, so the digest and
lease receipt carry over unchanged. It budgets
target resolution per validator tick: work exceeding the wall-clock resolution
budget defers to the next tick through the persisted
realized-target/`partially_scored` resumption path, so an archive-heavy 30d
horizon no longer holds a single tick open past the watchdog window. Wire
formats, resolved values, scoring math, and aggregation are unchanged. Its
watched-tree digest is
`3904a799a6560082a05b0ff62274cf4c71547cf1f5dfd0311418d2f4e574ef14`.
Its public lease authority receipt is SHA-256 over the UTF-8 lines
`LEASE_AUTHORITY`,
`PREVIOUS_RECEIPT=c4aa1b087b26039b30524093943467ae5074939aaf35c43c87fcb79ffc66ae13`,
`CURRENT_VERSION_KEY=30`, and
`CURRENT_VERSION_DIGEST=3904a799a6560082a05b0ff62274cf4c71547cf1f5dfd0311418d2f4e574ef14`,
each terminated by one LF byte. The resulting receipt is
`77afcb26d890245818340f23cb191c3192d682664006fd9bf2d9251a7537a304`.
No private ledger value is involved in any lease authority receipt.

Key `30` is recorded as `activation-0043`. It first appeared on the public
first-parent staging lineage in commit
`90c973f7a369746a4b19a8b4eb04fed2d37e4caa`; its source-bound receipt is
`86150d44b3134f1a10fe4a98d4bafda63eb6aa55c9d348ec0284bb83c2d384fc`.

Key `2041` was leased to the SN30 qualification candidate. Signed commit and
reveal requests bind all request fields, and rejected reveal persistence is
bounded by admission while accepted retries remain idempotent. Miners and
validators must upgrade together. The candidate line also carries the
mainnet-launch universe refresh: the Alpha Risk whitelist becomes the
15-netuid tuple in `endure/assessment/subnet_alpha_universe.py`, selected as
the top pools by TAO reserve plus operator picks, including this subnet's own
netuid 30 rated under the same rules. The key was unserved on every network
when the refresh folded in, so the lease re-folded at `2041` with a new
digest instead of burning a key. Its watched-tree digest is
`570989ed4a3e73bc1283e99742c3931712ec0e3c4aebc96a5aebcc1375ea87f7`. Every
re-fold of an unserved lease chains from the last activated record, key
`30`, never from an earlier fold of the same lease. The public lease
authority receipt uses
`PREVIOUS_RECEIPT=77afcb26d890245818340f23cb191c3192d682664006fd9bf2d9251a7537a304`,
`CURRENT_VERSION_KEY=2041`, and
`CURRENT_VERSION_DIGEST=570989ed4a3e73bc1283e99742c3931712ec0e3c4aebc96a5aebcc1375ea87f7`
under the `LEASE_AUTHORITY` format above, producing
`27e8f797e62ce76333067470e18a32bdccdd80a385235b4d700d21513880c2b1`.

The jump from `30` to `2041` deliberately clears the observed SN30 on-chain
minimum of `2040`; intermediate application keys need not be deployed. Stage
the complete release on testnet first, then promote the same source and protocol
key to mainnet after qualification. The candidate opens the mainnet serving
gate behind the explicit `--endure.serving_stage mainnet` acknowledgement.
This lease does not change either chain's configuration, start mainnet
serving on any deployment, or qualify a release by itself. Wallet hotkeys remain distinct
per environment; a protocol version key is not a wallet key.

Key `2041` also excludes retired Alpha Risk coordinates from active scoring and
consensus weights while preserving retired EMA memory and historical round
settlement. Reintroduction resumes preserved scores under the existing
registration rules; validators clear cached scores when no eligible scores remain. See the [universe-change policy](specs/2026-07-20-scoring-fairness-deltas.md#universe-changes)
and [upgrade and rollback guidance](deploy/operator-node.md#rollback).

Key `2041` is recorded as `activation-0044`. It first appeared on the public
first-parent staging lineage in commit
`824f1367f4d23cb4cee6605d53db709bd86bdd23`; its source-bound receipt is
`0df17c6368167b0f8b3f376b2d84e5f9810d89860c67532dfaef348a2559918d`.
The published `v0.1.0` images retain that assignment.

Key `2042` is leased to the SN30 correctness and owner-vote fallback cutover.
It pins mainnet admission to zero additional miner stake, commit/reveal caps of
10, and a 100-block metagraph/weight-attempt epoch. Admission and snapshot closure
serialize in SQLite, empty frozen snapshots never backfill during reads, and
commit retries validate the key and window. Storage admission/selection, miner
axon admission (registered hotkey and stake floor, in
[admission.py](../endure/protocol/admission.py)), deregistration confirmation
over `DEREGISTRATION_CONFIRMATION_SYNCS = 2` metagraph resyncs (in
[consensus_policy.py](../endure/protocol/consensus_policy.py), tracked by
[eligibility.py](../endure/scoring/eligibility.py), which also selects each
tick's scoring set of expected miners and archived hotkeys), the complete Decimal
score-to-u16 composition (abstain on no positive score, chain limits, u16
encoding, in [weight_processing.py](../endure/scoring/weight_processing.py)),
emission planning from one chain snapshot and its pre-submission recheck (in
[emission_policy.py](../endure/scoring/emission_policy.py)), Alpha
market-data sampling decisions (canonical sample blocks, timestamp-to-block
boundary searches, the series gap policy, the scoring retry budget, and the
missing-value and gap-versus-outage classification of archive reads, in
[market_sampling.py](../endure/scoring/market_sampling.py), moved unchanged
from `endure/live/alpha_market_data.py`; old-vs-new differentials over 4,500
and 4,000 cases found 0 mismatches), the mainnet/testnet genesis identities,
which schema is served, and the owner-vote fallback policy are
digest-covered. On mainnet
SN30 and Bittensor testnet, one emission-enabled process submits its whole vote
to the UID of the on-chain subnet owner hotkey whenever no score is positive,
and earned weights as soon as any score is positive, without a flag change or
restart; there is no latch. Mainnet additionally pins the genesis, netuid `30`,
and owner hotkey.
Explicitly disabling emission remains a true off switch. Startup teardown and
existing health/log observability are hardened in the same release.
No schema migration is introduced; scoring coefficients and the target universe
are unchanged. This remains one unserved `2042` lease, not another key bump.

Its watched-tree digest is
`eb63f6f3d2f87a6e1cffad8adc8135280a643313f17267c5b2b4770918330195`.
The public lease authority receipt uses
`PREVIOUS_RECEIPT=27e8f797e62ce76333067470e18a32bdccdd80a385235b4d700d21513880c2b1`,
`CURRENT_VERSION_KEY=2042`, and
`CURRENT_VERSION_DIGEST=eb63f6f3d2f87a6e1cffad8adc8135280a643313f17267c5b2b4770918330195`
under the `LEASE_AUTHORITY` format above, producing
`69fd9bc6b7064868e2244a507fde0a17303e718fa876f4c23ad38115240f39d1`.

Miners and validators must upgrade together. This source update does not publish
production images, deploy services, or raise the chain's weight-version floor.
SN30's chain `weights_version` is `2040` and Subtensor accepts a `version_key`
at or above it, so key-`2042` submissions are accepted without a
`weights_version` change. Raising it is a later, deliberate owner decision only
after every permit validator runs `2042`; raising it earlier would reject
validators still on `2040`.
Follow the [coordinated cutover](running_on_mainnet.md#coordinated-cutover) after
qualification and agreement with independently operated validators.
