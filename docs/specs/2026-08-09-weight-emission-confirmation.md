# Durable weight-emission confirmation

> **Status: current.**

This design replaced an in-memory confirmation check with a durable,
restart-safe weight-emission ledger.

## Problem

The SDK returning success from `set_weights(wait_for_inclusion=False)` proves
only that the RPC accepted a submission attempt. It does not prove that the
chain included the weights.

The former implementation stored one pending submission block in process memory
and compared it with the validator UID's later `last_update`. That state is lost on restart,
can be overwritten by a later submission, and has no deadline for declaring an
unconfirmed emission. Operators therefore cannot always answer the key
question: did these weights reach the chain?

## Goal

Make every weight-emission attempt durable and distinguish these facts:

| State | Meaning |
|---|---|
| `prepared` | The complete intent is durable before the SDK is invoked. |
| `submitted` | The RPC accepted the one-shot SDK call; chain inclusion is unknown. |
| `ambiguous` | The SDK was invoked but raised after it may have submitted. |
| `confirmed` | Finalized chain state proves that the recorded validator identity applied the exact persisted vector. |
| `unconfirmed` | The safe retry boundary elapsed without exact inclusion proof, including an observed CR4 commitment that never applied the vector. |
| `failed` | The SDK definitely did not submit or explicitly rejected the call. |

No row or health field may call a submission `emitted` or successful on-chain
until it is `confirmed`.

## Design

### Protocol identity

New attempts bind `CURRENT_VERSION_KEY` into the canonical intent identity and
pass that same key to the SDK. Package specification version `1000` is package
metadata and is never a
weight-emission identity. The stored form is `<key>:<blake2b-256>`, with the
version key also included in the canonical JSON preimage.

Legacy unprefixed 64-hex intent hashes remain readable and confirmable under
their original identity. A versioned row using any key other than the explicit
current runtime key cannot confirm. It remains open before its durable deadline
and becomes `unconfirmed` when that deadline passes.

Insertion and confirmation resolution read that current key inside the trusted
storage repository boundary. Their public APIs expose no caller-selectable
expected-key override; transport and neuron callers cannot authorize a retired
or otherwise consumed protocol key.

### Durable emission ledger

Add a forward-only database migration to `weight_emission_batches`. A batch
records:

- `submission_block` — fresh, uncached chain block sampled immediately before
  the SDK submission;
- `confirmation_state` — one of `prepared`, `submitted`, `ambiguous`,
  `confirmed`, `unconfirmed`, or `failed`;
- `confirmed_at` — UTC timestamp set only on confirmation;
- `baseline_last_update_block` — the validator UID's coherent pre-attempt
  `last_update` value;
- `period_blocks` — the explicit SDK extrinsic mortality;
- `confirmation_deadline_block` — a pre-invocation submission block plus
  mortality and finality margin, optionally extended by a fresher post-call
  sample; this remains the inclusion deadline for CR4;
- `cr4_reveal_deadline_block` — for CR4, the first block of the epoch after the
  complete reveal window plus finality margin, derived from the finalized epoch
  schedule, tempo, and `RevealPeriodEpochs` before submission;
- `chain_identity`, `netuid`, `validator_uid`, and `validator_hotkey` — the
  exact chain and signer context that prepared the intent;
- `intent_hash` — a canonical hash over the current protocol key, chain and
  validator identity, and emitted uint16 UID/hotkey/weight targets;
- `submission_mode` — direct or CR4;
- `commitment_hash`, `reveal_round`, `commitment_observed_block`, and
  `commitment_observed_last_update_block` — CR4 lifecycle evidence when the SDK
  returns it and the finalized commitment is first observed.

The existing batch status remains the transport outcome for compatibility. The
new confirmation state is the on-chain truth. Existing historical batches are
left unchanged and expose no fabricated confirmation result.

A protocol-keyed startup-fence table records the first safe submission block
for each schema and protocol lease. Restarts reuse that durable fence instead
of opening a fresh reveal window. Migration reconciles older open CR4 rows that
lack a reveal deadline to their conservative confirmation deadline, so they can
expire safely instead of blocking submissions forever.

### Submission and confirmation flow

1. The validator validates that its UID is in range and maps to its wallet
   hotkey, then captures the UID's current `last_update`, genesis hash, netuid,
   submission mode, signer identity, and canonical vector hash.
2. It samples a fresh submission block and atomically writes the complete batch
   and rows as `prepared`. Direct mode records a mortality-plus-finality deadline.
   CR4 records that inclusion deadline separately from its schedule-derived reveal
   deadline. A write failure prevents the SDK call.
3. It makes one SDK attempt (`max_attempts=1`) with both wait flags false and
   an explicit 128-block mortality period and the same current protocol key
   used by the canonical intent identity.
4. It conditionally transitions that exact prepared batch:
   - SDK success → `submitted`, including CR4 commitment/reveal metadata when
     applicable;
   - provider throttle or other post-invocation uncertainty → `ambiguous`;
   - local gate deferral or explicit rejection → `failed`.
   If the transition write fails, `prepared` remains open and blocks retries.
5. The database permits at most one `prepared`, `submitted`, or `ambiguous`
   batch per schema.
6. After an invoked SDK call, a fresh post-call block sample may extend the
   conservative inclusion deadline within a finite second-mortality bound. It
   never shortens or replaces the CR4 reveal deadline.
7. Resolution queries finalized canonical chain state, not the latest
   metagraph head:
   - the persisted chain, netuid, signer UID/hotkey, and every weighted target
     UID/hotkey must match identities queried at the same finalized block;
   - a versioned persisted intent hash must use the explicit current protocol
     key and match the stored rows; a legacy unprefixed hash uses the legacy
     identity;
   - direct mode confirms only when `last_update` advanced within mortality and
     finalized raw weights equal the exact stored vector;
   - CR4 records the matching commitment and reveal round whose commit block
     falls within the actual post-call inclusion window, recovering that metadata
     from a unique finalized signer commitment when the SDK response omitted it,
     plus the finalized `last_update` at observation;
    - CR4 confirms only when finalized `TimelockedWeightsRevealed` evidence for
      the recorded subnet and validator falls after submission or the observed
      commitment and within the reveal deadline, and finalized target
      identities and weights equal the exact persisted vector. `last_update`
      alone is not CR4 reveal proof because it advances when the commitment is
      accepted. A durable cursor scans finalized history as it becomes available
      through the reveal deadline in bounded 32-block batches, checkpointing each
      successful batch so downtime cannot hide a successful reveal or restart the
      full history scan. Each metagraph resync budgets enough batches for the
      configured resync interval plus one catch-up batch, preventing the normal
      interval from accumulating a deadline-sized backlog while keeping each
      resync bounded. A partial scan cannot expire the open attempt;
    - a direct attempt past its safe deadline or an unobserved CR4 commitment
      past its inclusion deadline becomes `unconfirmed`. An observed CR4 commit
      remains open while the finalized commitment is still pending and becomes
      `unconfirmed` only after its schedule-derived reveal deadline passes, the
      commitment is no longer visible, and exact application is absent;
   - prepared or ambiguous CR4 attempts without a unique matching commitment or
     finalized reveal event become `unconfirmed` when their durable deadline
     passes. Releasing single-flight remains health-degraded until a later
     confirmed emission, making the residual retry risk operator-visible.
8. After restart, finalized-state refreshes resume resolution from the durable
   ledger. Before a first upgraded submission, live startup waits through the
   CR4 reveal window (or one mortality-plus-finality window in direct mode) so
   an untracked legacy extrinsic cannot overlap.

The implementation may retain an SDK transaction hash when the SDK exposes a
stable one, but confirmation semantics do not depend on parsing a provider
message for a hash.

### Health and evidence semantics

`/live` reports process liveness only. `/health` reports operational readiness:

- last confirmed weights timestamp;
- count and age of open prepared/submitted/ambiguous batches;
- latest unconfirmed batch block;
- process-local consecutive failures and durable total failed submissions.

It returns degraded/503 while the RPC gate is actively deferred, consecutive
weight failures remain unresolved, an open attempt is overdue, or the latest
unconfirmed attempt is newer than the latest confirmation. Historical
cumulative failure totals alone do not keep health degraded forever.

Validator evidence exports preserve the transport `status`, identity-neutral
chain metadata, submission mode, and confirmation state. The local preparation
timestamp is named `attempted_at`, never `emitted_at`. Row-level `emitted` is
replaced or supplemented by an
unambiguous `confirmed` indicator so consumers cannot mistake an RPC submission
for chain inclusion. A row is confirmed/emitted only when its batch is
confirmed and it has a non-null emitted uint16 weight. Validator-local
`confirmed_at` timestamps are exported for operations but excluded from
cross-validator comparison.

## Acceptance tests

1. A durable `prepared` batch exists before the SDK is invoked and transitions
   to `submitted` after success.
2. A finalized exact vector plus an in-mortality direct `last_update` confirms
   the correct batch and sets `confirmed_at`.
3. Restarting the validator preserves and later resolves a submitted batch.
4. Two open submissions cannot coexist or overwrite each other.
5. A batch crossing its block deadline becomes `unconfirmed` without being
   reported as confirmed.
6. Evidence and `/health` distinguish all transport and confirmation states,
   including ambiguous attempts and durable failures.
7. Existing historical rows and exports remain readable after migration.
8. A provider throttle after invocation remains open and is never retried as a
   definite non-submission.
9. Invalid or mismatched validator UIDs cannot resolve another validator's
   batch.
10. A stale snapshot cannot shorten mortality, and a post-mortality update
    cannot confirm an expired direct attempt or establish CR4 commitment inclusion.
11. CR4 commitment inclusion remains open until a finalized
    `TimelockedWeightsRevealed` event and the exact target identities/vector
    confirm application. A commit-time `last_update` and pre-existing matching
    vector cannot confirm. Without reveal proof, the schedule-derived reveal
    deadline closes the batch as `unconfirmed`.
12. Active RPC backoff, consecutive emission failure, and overdue durable work
    return degraded/503 health.
13. Every open attempt has a pre-invocation deadline, and metadata-less CR4
    attempts either recover one unique signer commitment, prove the finalized
    reveal event and exact vector, or expire unconfirmed.
14. CR4 application after direct extrinsic mortality can still confirm from a
    finalized in-window reveal event and exact vector when downtime hid the
    commitment lifecycle; direct mortality does not cap reveal.
15. Production intent preparation and the SDK both use `CURRENT_VERSION_KEY`
    while package specification version `1000` remains unchanged.
16. Self-consistent versioned identities for keys `24` or `1000` are rejected
    for new attempts and never confirm; at deadline they become `unconfirmed`.
17. Legacy unprefixed intent hashes remain readable and confirmable.

## Rollout

1. Approve this migration design before implementation.
2. Implement as a separate PR against the hardened `develop` gate.
3. Run `make migrations`, `make verify`, and CI.
4. Promote to staging using a merge-commit PR and verify a full
   submit → confirm cycle plus a restart while a batch is submitted.
5. Do not promote to `main` until staging shows confirmed weights and no
   unresolved batches past their deadline.

## Non-goals

- Waiting synchronously for extrinsic inclusion or finalization.
- Retrying an ambiguous accepted submission immediately.
- Changing scoring values when an emission is unconfirmed.
- Assuming a particular RPC provider or a provider-specific transaction hash
  format.
