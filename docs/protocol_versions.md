# Protocol activation history

The machine-readable registry at
[`endure/protocol/activated_versions.json`](../endure/protocol/activated_versions.json)
separates retired activation history from the single unactivated current lease.
An activation is the first appearance of a distinct current protocol key and
digest pair on the first-parent staging lineage. Historical keys may repeat or
skip because each distinct activated assignment is recorded chronologically.

Public activation evidence and lease authority are opaque lowercase SHA-256
receipts. Their private preimages and source mappings remain in the release
evidence ledger and are not published, except where a lease explicitly records
a public reproducible preimage, as key `27` does below. The protocol contract
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

Protocol key `27` is leased exclusively to the v0.1.0 candidate but is not yet
an activated history record. The previous activation is key `26` with digest
`0d0153828eebe5f449b365b7b3a3c43e87f3118770a2f76dfb98637a6eed6d9e`, recorded as
`activation-0039` from its staging promotion merge
`f19f579fb3996289465616c537afbf95f17e2e7b`.

Key `27` carries the complete deletion of the legacy bank-risk reference
vertical. Its watched-tree digest is
`d0884ffa6bf8d98807d20ab9ee8a7a0c2821bb08d0cc6376fb87a6db605cf0fb`.
Unlike earlier leases, its authority receipt has a public preimage: SHA-256 over
the UTF-8 lines `LEASE_AUTHORITY`, `PREVIOUS_RECEIPT=<previous activation's
evidence_sha256>`, `CURRENT_VERSION_KEY=27`, and `CURRENT_VERSION_DIGEST=<the
digest above>`, each terminated by one LF byte. Anyone can recompute it from
this repository; no private ledger value is involved.
