"""Axon submission handlers: synapse → verdict → storage (spec §6).

The thin binding between the wire and the pure verdict functions. State comes
from the repository, time from an injected clock, and every reply mutates the
synapse's ``accepted``/``rejection_code`` in place — the bittensor axon
returns the same synapse to the caller.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from endure.assessment.registry import SchemaRegistry
from endure.protocol.round_engine import RoundWindows
from endure.protocol.synapses import RejectionCode, SubmitCommit, SubmitReveal
from endure.protocol.validation import (
    Verdict,
    is_canonical_round_id,
    reveal_payload_rejection,
    validate_commit,
    validate_reveal,
)
from endure.protocol.version_contract import CURRENT_VERSION_KEY
from endure.storage.repository import Storage

DEFAULT_MAX_COMMITS_PER_ROUND = 10
DEFAULT_MAX_REVEALS_PER_ROUND = 10


class SubmissionHandlers:
    def __init__(  # noqa: PLR0913 — separate commit/reveal controls are intentional
        self,
        *,
        storage: Storage,
        schema_id: str,
        now_fn: Callable[[], datetime],
        max_commits_per_round: int = DEFAULT_MAX_COMMITS_PER_ROUND,
        max_reveals_per_round: int = DEFAULT_MAX_REVEALS_PER_ROUND,
        registry: SchemaRegistry | None = None,
    ) -> None:
        self._storage = storage
        self._schema_id = schema_id
        self._now_fn = now_fn
        self._max_commits = max_commits_per_round
        self._max_reveals = max_reveals_per_round
        self._registry = registry

    @staticmethod
    def _apply(synapse: SubmitCommit | SubmitReveal, verdict: Verdict) -> None:
        synapse.accepted = verdict.accepted
        synapse.rejection_code = (
            None if verdict.rejection_code is None else verdict.rejection_code.value
        )

    @staticmethod
    def _reject(synapse: SubmitCommit | SubmitReveal, code: RejectionCode) -> None:
        synapse.accepted = False
        synapse.rejection_code = code.value

    def _validate_reveal(  # noqa: PLR0913 — explicit validation context
        self,
        synapse: SubmitReveal,
        *,
        committed: str | None,
        miner_hotkey: str,
        now: datetime,
        windows: RoundWindows,
        universe_tickers: tuple[str, ...],
    ) -> Verdict:
        return validate_reveal(
            round_id=synapse.round_id,
            schema_id=synapse.schema_id,
            spec_version=synapse.spec_version,
            bundle_json=synapse.bundle_json,
            nonce_hex=synapse.nonce_hex,
            committed_hash=committed,
            miner_hotkey=miner_hotkey,
            now=now,
            reveal_open=windows.reveal_open,
            reveal_close=windows.reveal_close,
            universe=universe_tickers,
            registry=self._registry,
        )

    def _admit_reveal(  # noqa: PLR0913 — explicit admission context
        self,
        synapse: SubmitReveal,
        *,
        committed: str | None,
        miner_hotkey: str,
        now: datetime,
        is_open_window: bool,
        windows: RoundWindows,
        universe_tickers: tuple[str, ...],
    ) -> Verdict:
        if not is_open_window or committed is None:
            return self._validate_reveal(
                synapse,
                committed=committed,
                miner_hotkey=miner_hotkey,
                now=now,
                windows=windows,
                universe_tickers=universe_tickers,
            )
        charged = self._storage.record_reveal_attempt(
            synapse.round_id,
            self._schema_id,
            miner_hotkey,
            max_reveals=self._max_reveals,
        )
        if not charged:
            return Verdict(accepted=False, rejection_code=RejectionCode.RATE_LIMITED)
        return self._validate_reveal(
            synapse,
            committed=committed,
            miner_hotkey=miner_hotkey,
            now=now,
            windows=windows,
            universe_tickers=universe_tickers,
        )

    async def handle_commit(
        self, synapse: SubmitCommit, *, miner_hotkey: str
    ) -> SubmitCommit:
        now = self._now_fn()
        # This handler serves exactly one schema; without this guard a synapse
        # for another registered schema would validate as that schema but be
        # stored under self._schema_id.
        if synapse.schema_id != self._schema_id:
            self._reject(synapse, RejectionCode.UNKNOWN_SCHEMA)
            return synapse
        # Validate round_id before it reaches a DB query: a non-canonical date
        # can split commits/reveals across storage keys, and an unbounded
        # string would otherwise be bound into the round_windows lookup.
        if not is_canonical_round_id(synapse.round_id):
            self._reject(synapse, RejectionCode.MALFORMED_BUNDLE)
            return synapse
        windows = self._storage.round_windows(synapse.round_id, self._schema_id)
        if windows is None:
            self._reject(synapse, RejectionCode.ROUND_UNAVAILABLE)
            return synapse
        # Advisory fast path: an over-cap miner is rejected before any
        # validation work (the cap doubles as a DoS control). An exact retry
        # is already committed, so acknowledge it without spending a slot.
        # The authoritative changed-hash check-and-increment stays atomic in
        # record_commit.
        commit_count = self._storage.commit_count(
            synapse.round_id, self._schema_id, miner_hotkey
        )
        is_idempotent_retry = commit_count >= self._max_commits and (
            self._storage.committed_hash(
                synapse.round_id, self._schema_id, miner_hotkey
            )
            == synapse.bundle_hash
        )
        if commit_count >= self._max_commits and not is_idempotent_retry:
            self._reject(synapse, RejectionCode.RATE_LIMITED)
            return synapse

        verdict = (
            Verdict(accepted=True)
            if is_idempotent_retry
            else validate_commit(
                round_id=synapse.round_id,
                schema_id=synapse.schema_id,
                spec_version=synapse.spec_version,
                bundle_hash=synapse.bundle_hash,
                now=now,
                commit_open=windows.commit_open,
                commit_close=windows.commit_close,
                registry=self._registry,
            )
        )
        if verdict.accepted and not is_idempotent_retry:
            # Rate check and increment are one transaction in storage —
            # concurrent commits cannot exceed the cap.
            recorded = self._storage.record_commit(
                synapse.round_id,
                self._schema_id,
                miner_hotkey,
                synapse.bundle_hash,
                now_iso=now.isoformat(),
                max_commits=self._max_commits,
            )
            if not recorded:
                self._reject(synapse, RejectionCode.RATE_LIMITED)
                return synapse
        self._apply(synapse, verdict)
        return synapse

    async def handle_reveal(
        self, synapse: SubmitReveal, *, miner_hotkey: str
    ) -> SubmitReveal:
        now = self._now_fn()
        if synapse.schema_id != self._schema_id:
            self._reject(synapse, RejectionCode.UNKNOWN_SCHEMA)
            return synapse
        if not is_canonical_round_id(synapse.round_id):
            self._reject(synapse, RejectionCode.MALFORMED_BUNDLE)
            return synapse
        early_rejection = (
            RejectionCode.VERSION_MISMATCH
            if synapse.spec_version != CURRENT_VERSION_KEY
            else reveal_payload_rejection(synapse.bundle_json, synapse.nonce_hex)
        )
        if early_rejection is not None:
            self._reject(synapse, early_rejection)
            return synapse
        windows = self._storage.round_windows(synapse.round_id, self._schema_id)
        if windows is None:
            self._reject(synapse, RejectionCode.ROUND_UNAVAILABLE)
            return synapse
        universe = self._storage.universe_for(synapse.round_id, self._schema_id)
        if universe is None:
            self._reject(synapse, RejectionCode.ROUND_UNAVAILABLE)
            return synapse
        is_open_window = windows.reveal_open <= now <= windows.reveal_close
        accepted_reveal = self._storage.accepted_reveal(
            synapse.round_id, self._schema_id, miner_hotkey
        )
        committed = self._storage.committed_hash(
            synapse.round_id, self._schema_id, miner_hotkey
        )
        if is_open_window and accepted_reveal == (
            synapse.bundle_json,
            synapse.nonce_hex,
        ):
            verdict = Verdict(accepted=True)
        else:
            verdict = self._admit_reveal(
                synapse,
                committed=committed,
                miner_hotkey=miner_hotkey,
                now=now,
                is_open_window=is_open_window,
                windows=windows,
                universe_tickers=universe.tickers,
            )
        if committed is not None:
            self._storage.record_reveal(
                synapse.round_id,
                self._schema_id,
                miner_hotkey,
                bundle_json=synapse.bundle_json,
                nonce_hex=synapse.nonce_hex,
                accepted=verdict.accepted,
                rejection_code=(
                    None
                    if verdict.rejection_code is None
                    else verdict.rejection_code.value
                ),
                now_iso=now.isoformat(),
            )
        self._apply(synapse, verdict)
        return synapse
