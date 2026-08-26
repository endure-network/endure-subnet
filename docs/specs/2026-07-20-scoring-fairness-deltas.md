# Alpha Risk absence-aware scoring and weight audit

> **Status: current.** Applies to the served Alpha Risk V1 schema.

This document records the two fairness and audit properties added under
protocol key `21` and migration `0009`. Current compatibility is always defined
by `endure/protocol/version_contract.py`.

## 1 — Absence-aware scoring

Skipping a round must not preserve a miner's previous accuracy indefinitely.
For each resolved coordinate, the scoring set is:

```text
hotkeys with active EMA state union hotkeys with an accepted reveal this round
```

Each miner in that set receives its measured coordinate score when present and
an exact zero observation when absent. A miner that has never submitted has no
EMA state and does not accumulate permanent zero-history rows.

### Registration and archival

Registration state is kept separate from the scoring set:

- a hotkey missing from one metagraph refresh remains in the scoring set and is
  zero-filled;
- absence across two consecutive metagraph generations confirms deregistration;
- confirmed-deregistered state is archived only after unfinished rounds can no
  longer require it;
- a continuously absent hotkey whose coordinate EMAs all fall below the
  consensus-weight floor is archived;
- re-registration after archival starts from a cold score state;
- emitted weights include only currently registered hotkeys.

Consensus remains based on the accepted bundle set and the unfiltered blended
scores captured for that round. Deregistration after a bundle was accepted does
not retroactively change its consensus weight.

### Resolution invariants

- Zero-fill applies only to coordinates with a resolved realized target.
- Voided or unavailable coordinates produce no score update.
- Each round/horizon scoring pass persists realized targets, coordinate scores,
  EMA changes, score history, and consensus atomically.
- A failed horizon is contained and retried without partially advancing the
  round.

## 2 — Weight-emission audit trail

Every validator weight attempt records a batch and its per-hotkey rows before
or alongside the transport result. The audit record preserves:

- schema, round and protocol identity;
- metagraph size and hotkey/UID mapping;
- raw blended score and normalized weight;
- processed and uint16 emission vectors;
- transport outcome and timestamp.

The Bittensor adapter exposes a transport-level hook; the concrete Endure
validator joins that data to domain identity and durable storage. Product logic
does not move into `endure/base`.

Transport success is not proof of chain inclusion. The durable confirmation
states and exact-vector evidence are defined in
[Durable weight-emission confirmation](2026-08-09-weight-emission-confirmation.md).

## Acceptance invariants

- An absent active miner decays rather than retaining a stale score.
- A never-active miner creates no score history until it submits.
- One missing metagraph observation cannot archive a miner.
- Deregistered and fully decayed miners cannot continue receiving weights.
- Every attempted emission has one auditable batch outcome.
- Only a confirmed batch is described as applied on-chain.
