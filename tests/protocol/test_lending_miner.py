from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from endure.assessment.lending_universe import FORGE_LENDING_WHITELISTED_NETUIDS
from endure.assessment.schemas.forge_lending import (
    FORGE_LENDING_SCHEMA_ID,
    LendingOutput,
    LendingSubmissionBundle,
)
from endure.protocol.bundles import assemble_bundle
from endure.protocol.canonical import canonical_bundle_bytes, commit_hash
from endure.protocol.lending_miner import (
    LendingBaselineAssembler,
    baseline_lending_bundle,
)
from endure.protocol.miner_service import MinerRoundService
from endure.protocol.schedulers import SyntheticScheduler
from endure.protocol.synapses import SubmitCommit, SubmitReveal

ROUND = "2026-07-06"
EPOCH = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
PERIOD = 100


def _lending_miner_service(
    sent: list[object],
    now_holder: dict[str, datetime],
    *,
    accept: dict[str, int] | None = None,
) -> MinerRoundService:
    acceptances = accept if accept is not None else {"n": 1}

    async def send(synapse: SubmitCommit | SubmitReveal) -> int:
        sent.append(synapse)
        return acceptances["n"]

    return MinerRoundService(
        scheduler=SyntheticScheduler(
            sessions=(date(2026, 7, 6),), epoch=EPOCH, period_seconds=PERIOD
        ),
        assemble=LendingBaselineAssembler(
            netuids=FORGE_LENDING_WHITELISTED_NETUIDS,
            miner_hotkey="hk-miner",
        ),
        send=send,
        now_fn=lambda: now_holder["now"],
    )


class TestBaselineLendingBundle:
    def test_builds_valid_bundle_for_all_whitelisted_netuids(self) -> None:
        bundle = baseline_lending_bundle(
            round_id=ROUND, netuids=FORGE_LENDING_WHITELISTED_NETUIDS
        )

        assert isinstance(bundle, LendingSubmissionBundle)
        assert bundle.round_id == ROUND
        assert bundle.schema_id == FORGE_LENDING_SCHEMA_ID
        assert {asset.netuid for asset in bundle.assets} == set(
            FORGE_LENDING_WHITELISTED_NETUIDS
        )

    def test_baseline_is_deterministic(self) -> None:
        first = baseline_lending_bundle(round_id=ROUND, netuids=(44, 8))
        second = baseline_lending_bundle(round_id=ROUND, netuids=(8, 44))

        assert canonical_bundle_bytes(
            first.to_canonical_payload()
        ) == canonical_bundle_bytes(second.to_canonical_payload())

    def test_baseline_is_conservative_per_asset(self) -> None:
        bundle = baseline_lending_bundle(round_id=ROUND, netuids=(44,))

        (asset,) = bundle.assets
        values = {item.output: item.value for item in asset.outputs}
        assert (
            values[LendingOutput.COLLATERAL_FACTOR]
            <= values[LendingOutput.LIQUIDATION_THRESHOLD]
        )
        assert values[LendingOutput.SUPPLY_CAP] == 0
        assert values[LendingOutput.BORROW_CAP] == 0
        assert values[LendingOutput.RISK_TIER] == 6


class TestAssembleBundle:
    def test_assembles_lending_bundle_with_canonical_hash(self) -> None:
        bundle = baseline_lending_bundle(round_id=ROUND, netuids=(44,))
        nonce = bytes(range(16))

        assembled = assemble_bundle(bundle, miner_hotkey="hk-miner", nonce=nonce)

        expected_bytes = canonical_bundle_bytes(bundle.to_canonical_payload())
        assert assembled.bundle_json == expected_bytes.decode("utf-8")
        assert assembled.nonce_hex == nonce.hex()
        assert assembled.bundle_hash == commit_hash(
            expected_bytes, nonce, miner_hotkey="hk-miner"
        )
        assert json.loads(assembled.bundle_json)["schema_id"] == (
            FORGE_LENDING_SCHEMA_ID
        )

    def test_generates_nonce_when_not_supplied(self) -> None:
        bundle = baseline_lending_bundle(round_id=ROUND, netuids=(44,))

        assembled = assemble_bundle(bundle, miner_hotkey="hk-miner")

        assert len(bytes.fromhex(assembled.nonce_hex)) == 16


class TestLendingMinerRoundService:
    async def test_lending_miner_commits_then_reveals_baseline_bundle(self) -> None:
        sent: list[object] = []
        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        service = _lending_miner_service(sent, now_holder)

        await service.tick()  # commit window → SubmitCommit
        now_holder["now"] = EPOCH + timedelta(seconds=60)
        await service.tick()  # reveal window → SubmitReveal

        commit, reveal = sent
        assert isinstance(commit, SubmitCommit)
        assert isinstance(reveal, SubmitReveal)
        assert commit.schema_id == reveal.schema_id == FORGE_LENDING_SCHEMA_ID
        assert commit.round_id == reveal.round_id == ROUND
        bundle = LendingSubmissionBundle.model_validate_json(reveal.bundle_json)
        assert {asset.netuid for asset in bundle.assets} == set(
            FORGE_LENDING_WHITELISTED_NETUIDS
        )
        assert commit.bundle_hash == commit_hash(
            reveal.bundle_json.encode("utf-8"),
            bytes.fromhex(reveal.nonce_hex),
            miner_hotkey="hk-miner",
        )

    async def test_missed_commit_is_terminal_and_observable(self) -> None:
        sent: list[object] = []
        now_holder = {"now": EPOCH + timedelta(seconds=10)}
        service = _lending_miner_service(sent, now_holder, accept={"n": 0})

        await service.tick()  # commit pushed but never accepted
        now_holder["now"] = EPOCH + timedelta(seconds=60)
        await service.tick()  # reveal window: nothing can be revealed
        await service.tick()  # recorded once, not per tick

        assert not any(isinstance(synapse, SubmitReveal) for synapse in sent)
        assert service.missed_commit_rounds == (ROUND,)


class TestSchemaIdSingleSource:
    async def test_service_derives_schema_id_from_the_assembler(self) -> None:
        # The assembler embeds schema_id inside the bundle it hashes, so the
        # service must take the wire schema_id from the assembler — a separate
        # constructor arg could diverge and produce accepted commits whose
        # reveals are all rejected.
        sent: list[object] = []
        now_holder = {"now": EPOCH + timedelta(seconds=10)}

        async def send(synapse: SubmitCommit | SubmitReveal) -> int:
            sent.append(synapse)
            return 1

        service = MinerRoundService(
            scheduler=SyntheticScheduler(
                sessions=(date(2026, 7, 6),), epoch=EPOCH, period_seconds=PERIOD
            ),
            assemble=LendingBaselineAssembler(
                netuids=FORGE_LENDING_WHITELISTED_NETUIDS,
                miner_hotkey="hk-miner",
            ),
            send=send,
            now_fn=lambda: now_holder["now"],
        )

        await service.tick()

        (commit,) = sent
        assert isinstance(commit, SubmitCommit)
        assert commit.schema_id == FORGE_LENDING_SCHEMA_ID
