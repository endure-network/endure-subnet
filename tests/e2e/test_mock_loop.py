"""Mock-mode E2E: commit → reveal → storage through the real handlers.

Runs the round lifecycle in-process with a compressed clock, real storage, real
handlers, and the deterministic baseline assembler — no chain, no sockets.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from endure.assessment.lending_universe import (
    FORGE_LENDING_WHITELISTED_NETUIDS,
    StaticLendingUniverseProvider,
)
from endure.assessment.registry import default_registry
from endure.assessment.schemas.forge_lending import (
    FORGE_LENDING_SCHEMA_ID,
    LendingSubmissionBundle,
)
from endure.protocol.handlers import SubmissionHandlers
from endure.protocol.lending_miner import LendingBaselineAssembler
from endure.protocol.miner_service import MinerRoundService
from endure.protocol.schedulers import SyntheticScheduler
from endure.protocol.synapses import SubmitCommit, SubmitReveal
from endure.storage.repository import Storage


class TestLendingMinerMockLoop:
    async def test_lending_miner_commits_and_reveals_through_handlers(
        self, storage: Storage
    ) -> None:
        """Walking-skeleton mock loop: a lending miner produces a valid
        baseline bundle for every whitelisted netuid and lands it in storage
        through the real commit/reveal handlers."""
        epoch = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
        scheduler = SyntheticScheduler(
            sessions=(date(2026, 7, 6),), epoch=epoch, period_seconds=100
        )
        now_holder = {"now": epoch + timedelta(seconds=10)}
        windows = scheduler.active_window(now_holder["now"])
        assert windows is not None
        provider = StaticLendingUniverseProvider()
        storage.open_round(
            windows=windows,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            universe=provider.fetch_universe(windows.round_id),
            now_iso=now_holder["now"].isoformat(),
        )
        handlers = SubmissionHandlers(
            storage=storage,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            now_fn=lambda: now_holder["now"],
            registry=default_registry(),
        )
        acks: list[tuple[str, bool, str | None]] = []

        async def send(synapse: SubmitCommit | SubmitReveal) -> int:
            if isinstance(synapse, SubmitCommit):
                response = await handlers.handle_commit(
                    synapse, miner_hotkey="hk-lending"
                )
            else:
                response = await handlers.handle_reveal(
                    synapse, miner_hotkey="hk-lending"
                )
            acks.append(
                (type(synapse).__name__, response.accepted, response.rejection_code)
            )
            return 1 if response.accepted else 0

        service = MinerRoundService(
            scheduler=scheduler,
            assemble=LendingBaselineAssembler(
                netuids=FORGE_LENDING_WHITELISTED_NETUIDS,
                miner_hotkey="hk-lending",
            ),
            send=send,
            now_fn=lambda: now_holder["now"],
        )

        await service.tick()  # commit window
        now_holder["now"] = epoch + timedelta(seconds=60)
        await service.tick()  # reveal window

        assert acks == [("SubmitCommit", True, None), ("SubmitReveal", True, None)]
        accepted = storage.accepted_bundles(windows.round_id, FORGE_LENDING_SCHEMA_ID)
        assert [hotkey for hotkey, _ in accepted] == ["hk-lending"]
        bundle = LendingSubmissionBundle.model_validate_json(accepted[0][1])
        assert {asset.netuid for asset in bundle.assets} == set(
            FORGE_LENDING_WHITELISTED_NETUIDS
        )
        assert service.missed_commit_rounds == ()
