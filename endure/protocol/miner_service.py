"""Miner round service: assemble → push (spec §2, §6).

Drives one miner through the daily cycle: build the bundle during the commit
window, push the commit, push the reveal after the close. Both ends are
seams: ``assemble`` maps a round id to a hashed submission (each vertical
binds it to its own deterministic baseline builder), and ``send`` is bound by
the neuron to a dendrite broadcast across validator axons reporting how many
accepted; tests bind it to a list. Two reliability rules: a round is only
marked committed/revealed once at least one validator accepted — zero
acceptances retry on the next tick while the window is open — and assembled
state (bundle, nonce, flags) persists to disk before any send, so a restart in
the commit→reveal gap can still reveal the exact committed preimage.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol

import bittensor as bt

from endure.protocol.bundles import AssembledSubmission
from endure.protocol.round_engine import RoundPhase, phase_at
from endure.protocol.schedulers import RoundScheduler
from endure.protocol.synapses import SubmitCommit, SubmitReveal
from endure.protocol.version_contract import CURRENT_VERSION_KEY

# ``send`` returns how many validators accepted the synapse.
Send = Callable[[SubmitCommit | SubmitReveal], Awaitable[int]]


class BundleAssembler(Protocol):
    """Maps a round id to the hashed submission for that round.

    Exposes the schema it assembles for: the assembler embeds ``schema_id``
    inside the bundle it hashes, so the service must stamp the same value on
    the commit/reveal wire — a second, independent source would let the two
    diverge, producing accepted commits whose reveals are all rejected.
    """

    @property
    def schema_id(self) -> str: ...

    def __call__(self, round_id: str) -> AssembledSubmission: ...


# Bound on persisted/tracked rounds; older rounds are long past their reveal
# window and can never be acted on again.
_MAX_TRACKED_ROUNDS = 10


class MinerRoundService:
    def __init__(
        self,
        *,
        scheduler: RoundScheduler,
        assemble: BundleAssembler,
        send: Send,
        now_fn: Callable[[], datetime],
        state_path: Path | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._assemble_fn = assemble
        self._schema_id = assemble.schema_id
        self._send = send
        self._now_fn = now_fn
        self._state_path = state_path
        self._assembled: dict[str, AssembledSubmission] = {}
        self._committed: set[str] = set()
        self._revealed: set[str] = set()
        self._missed_commits: set[str] = set()
        self._load_state()

    @property
    def missed_commit_rounds(self) -> tuple[str, ...]:
        """Rounds whose reveal window opened without any accepted commit.

        A missed commit is terminal — the reveal can never be accepted — so
        operators must be able to see the miner silently skipping rounds.
        """
        return tuple(sorted(self._missed_commits))

    # -- state persistence -------------------------------------------------

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            for round_id in sorted(payload)[-_MAX_TRACKED_ROUNDS:]:
                entry = payload[round_id]
                self._assembled[round_id] = AssembledSubmission(
                    bundle_json=entry["bundle_json"],
                    nonce_hex=entry["nonce_hex"],
                    bundle_hash=entry["bundle_hash"],
                )
                if entry["committed"]:
                    self._committed.add(round_id)
                if entry["revealed"]:
                    self._revealed.add(round_id)
        except Exception as error:  # noqa: BLE001 — resilience must not brick boot
            # Set the bad file aside and mine fresh: losing one round's nonce
            # is strictly better than a miner that cannot start. Uniquify the
            # quarantine name so a second corruption can't clobber the first
            # forensic copy.
            stamp = self._now_fn().strftime("%Y%m%dT%H%M%S")
            stem = self._state_path.stem
            quarantine = self._state_path.with_name(f"{stem}.corrupt-{stamp}")
            counter = 0
            while quarantine.exists():
                counter += 1
                quarantine = self._state_path.with_name(
                    f"{stem}.corrupt-{stamp}-{counter}"
                )
            os.replace(self._state_path, quarantine)
            self._assembled.clear()
            self._committed.clear()
            self._revealed.clear()
            bt.logging.error(f"corrupt miner state file moved to {quarantine}: {error}")

    def _persist_state(self) -> None:
        if self._state_path is None:
            return
        payload = {
            round_id: {
                "bundle_json": self._assembled[round_id].bundle_json,
                "nonce_hex": self._assembled[round_id].nonce_hex,
                "bundle_hash": self._assembled[round_id].bundle_hash,
                "committed": round_id in self._committed,
                "revealed": round_id in self._revealed,
            }
            for round_id in sorted(self._assembled)[-_MAX_TRACKED_ROUNDS:]
        }
        temporary = self._state_path.with_suffix(".tmp")
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        # Owner-only from creation: the file holds the pre-reveal bundle and
        # nonce, whose secrecy is the whole point of commit-reveal. Create it
        # 0600 via os.open(O_CREAT|O_EXCL) rather than writing then chmod'ing —
        # the latter leaves a brief window where the temp file is group/other
        # readable. A stale temp from a prior crash is removed first.
        temporary.unlink(missing_ok=True)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload))
        os.replace(temporary, self._state_path)

    # -- round actions -------------------------------------------------------

    def _assemble(self, round_id: str) -> AssembledSubmission:
        assembled = self._assembled.get(round_id)
        if assembled is not None:
            return assembled
        assembled = self._assemble_fn(round_id)
        self._assembled[round_id] = assembled
        # The nonce must be durable BEFORE any send: a commit may land on
        # validators even if this process dies mid-broadcast.
        self._persist_state()
        return assembled

    async def tick(self) -> None:
        """Advance the miner one step.

        Commit and reveal are re-pushed every tick while their window is open:
        ``send`` targets only validators that have not yet acked, so a single
        early ack does not stop distribution to the rest. ``_committed`` /
        ``_revealed`` gate progression (reveal needs ≥1 commit ack); once every
        serving validator holds the submission ``send`` becomes a no-op.
        """
        now = self._now_fn()
        window = self._scheduler.active_window(now)
        if window is None:
            return
        phase = phase_at(now, window)
        if phase is RoundPhase.COMMIT:
            await self._tick_commit(window.round_id)
        elif phase is RoundPhase.REVEAL:
            await self._tick_reveal(window.round_id)

    async def _tick_commit(self, round_id: str) -> None:
        assembled = self._assembled.get(round_id)
        if assembled is None:
            # Assembly may read market data and always writes state to disk —
            # both blocking. Run them off the event loop so the axon/dendrite
            # coroutines keep serving meanwhile.
            assembled = await asyncio.to_thread(self._assemble, round_id)
        accepted = await self._send(
            SubmitCommit(
                round_id=round_id,
                schema_id=self._schema_id,
                spec_version=CURRENT_VERSION_KEY,
                bundle_hash=assembled.bundle_hash,
            )
        )
        if accepted >= 1:
            if round_id not in self._committed:
                self._committed.add(round_id)
                self._persist_state()
                bt.logging.info(f"committed round {round_id} ({accepted} validators)")
        else:
            bt.logging.warning(
                f"round {round_id}: no validator accepted the commit — "
                "retrying next tick while the window is open"
            )

    async def _tick_reveal(self, round_id: str) -> None:
        if round_id not in self._committed:
            if round_id not in self._missed_commits:
                self._missed_commits.add(round_id)
                if len(self._missed_commits) > _MAX_TRACKED_ROUNDS:
                    self._missed_commits.discard(min(self._missed_commits))
                bt.logging.warning(
                    f"round {round_id}: reveal window open but no validator "
                    "accepted a commit — this round's submission is lost"
                )
            return
        assembled = self._assembled[round_id]
        accepted = await self._send(
            SubmitReveal(
                round_id=round_id,
                schema_id=self._schema_id,
                spec_version=CURRENT_VERSION_KEY,
                bundle_json=assembled.bundle_json,
                nonce_hex=assembled.nonce_hex,
            )
        )
        if accepted >= 1:
            if round_id not in self._revealed:
                self._revealed.add(round_id)
                self._persist_state()
                bt.logging.info(f"revealed round {round_id} ({accepted} validators)")
        else:
            bt.logging.warning(
                f"round {round_id}: no validator accepted the reveal — "
                "retrying next tick while the window is open"
            )
