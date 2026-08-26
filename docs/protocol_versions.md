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

Key `28` is leased exclusively to `release/v0.1.0-rc.1`. It carries the bounded
live-sampling fix, missing-timestamp settlement, round-aware miner eligibility,
and the next-commit-close publication embargo. Its watched-tree digest is
`05da1df37dc67de435d0954d9b102be45922c6956822643ff1dcc7a892176e26`.

The key-`28` authority receipt is publicly reproducible: SHA-256 over the UTF-8
lines `LEASE_AUTHORITY`,
`PREVIOUS_RECEIPT=cf3226d57dc49d5f84fed5d8eb79676c2fd215c082de214202b9a368571be5e9`,
`CURRENT_VERSION_KEY=28`, and
`CURRENT_VERSION_DIGEST=05da1df37dc67de435d0954d9b102be45922c6956822643ff1dcc7a892176e26`,
each terminated by one LF byte. The resulting receipt is
`fa41045b844d60c22340a5ed0fd8118cc53c83031490eea755c2cdb05c9ccd71`.
No private ledger value is involved.
