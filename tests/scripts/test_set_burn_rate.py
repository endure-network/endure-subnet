"""The owner burn-rate tool reads and publishes only the owner's commitment."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from endure.protocol.consensus_policy import MAINNET_GENESIS_HASH, SN30_OWNER_HOTKEY
from scripts.set_burn_rate import main
from tests.scoring.test_emission_policy import commitment_record

OWNER = "owner-hotkey"


class FakeChain:
    def __init__(
        self, commitment: str | None = None, *, genesis: str = "0xtestnet"
    ) -> None:
        self.genesis = genesis
        self.owner = OWNER
        self.records: dict[str, object] = {}
        if commitment is not None:
            self.records[OWNER] = commitment_record(commitment)
        self.published: list[tuple[int, str]] = []
        self.publish_succeeds = True
        self.closed = False
        self.block = 500
        self.read_blocks: list[int] = []

    def get_current_block(self) -> int:
        self.block += 1  # The head moves between calls.
        return self.block

    def get_metagraph_info(
        self, netuid: int, *, selected_indices: list[int], block: int
    ) -> SimpleNamespace:
        assert selected_indices == [0, 5, 7]
        self.read_blocks.append(block)
        return SimpleNamespace(netuid=netuid, block=block, owner_hotkey=self.owner)

    def get_block_hash(self, block: int) -> str:
        assert block == 0
        return self.genesis

    def get_commitment_metadata(
        self, netuid: int, hotkey_ss58: str, block: int
    ) -> object:
        self.read_blocks.append(block)
        return self.records.get(hotkey_ss58, "")

    def set_commitment(
        self, wallet: SimpleNamespace, netuid: int, data: str, **kwargs: object
    ) -> SimpleNamespace:
        assert kwargs["wait_for_finalization"] is True
        self.published.append((netuid, data))
        if self.publish_succeeds:
            self.records[wallet.hotkey.ss58_address] = commitment_record(data)
        return SimpleNamespace(success=self.publish_succeeds, message="rejected")

    def close(self) -> None:
        self.closed = True


def _run(chain: FakeChain, *argv: str, signer: str = OWNER, netuid: int = 30) -> int:
    return main(
        ["--netuid", str(netuid), *argv],
        subtensor_factory=lambda _network: chain,
        wallet_factory=lambda **_kwargs: SimpleNamespace(
            hotkey=SimpleNamespace(ss58_address=signer)
        ),
    )


def test_reading_reports_the_rate_validators_apply(
    capsys: pytest.CaptureFixture[str],
) -> None:
    chain = FakeChain("endure.burn_bps=9850")

    assert _run(chain) == 0
    output = capsys.readouterr().out
    assert "burn rate:    9850 bps (98.50% to the owner)" in output
    assert chain.published == [] and chain.closed


def test_reading_without_a_commitment_reports_the_full_burn(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run(FakeChain()) == 0
    output = capsys.readouterr().out
    assert "commitment:   none" in output
    assert "burn rate:    10000 bps (100.00% to the owner)" in output


def test_publishing_signs_the_exact_commitment_and_reads_it_back(
    capsys: pytest.CaptureFixture[str],
) -> None:
    chain = FakeChain()
    exit_code = _run(
        chain, "--publish", "9800", "--wallet-name", "owner", "--wallet-hotkey", "hk"
    )

    assert exit_code == 0
    assert chain.published == [(30, "endure.burn_bps=9800")]
    assert "published:    9800 bps (98.00%)" in capsys.readouterr().out


def test_publishing_refuses_any_hotkey_but_the_owner() -> None:
    chain = FakeChain()
    exit_code = _run(
        chain,
        "--publish",
        "0",
        "--wallet-name",
        "other",
        "--wallet-hotkey",
        "hk",
        signer="not-the-owner",
    )

    assert exit_code == 1
    assert chain.published == []


def test_a_rejected_publish_fails_loudly() -> None:
    chain = FakeChain("endure.burn_bps=9800")
    chain.publish_succeeds = False
    exit_code = _run(
        chain, "--publish", "9000", "--wallet-name", "owner", "--wallet-hotkey", "hk"
    )

    assert exit_code == 1
    assert chain.records[OWNER] == commitment_record("endure.burn_bps=9800")


@pytest.mark.parametrize(
    "argv",
    [
        ["--publish", "10001", "--wallet-name", "owner", "--wallet-hotkey", "hk"],
        ["--publish", "-1", "--wallet-name", "owner", "--wallet-hotkey", "hk"],
        ["--publish", "9800"],
    ],
)
def test_an_unpublishable_request_never_reaches_the_chain(argv: list[str]) -> None:
    chain = FakeChain()
    with pytest.raises(SystemExit) as exited:
        _run(chain, *argv)

    assert exited.value.code == 2
    assert chain.published == []


PUBLISH = ("--publish", "9800", "--wallet-name", "owner", "--wallet-hotkey", "hk")


def test_mainnet_publishing_requires_the_pinned_sn30_owner(
    capsys: pytest.CaptureFixture[str],
) -> None:
    pinned = FakeChain(genesis=MAINNET_GENESIS_HASH)
    pinned.owner = SN30_OWNER_HOTKEY
    assert _run(pinned, *PUBLISH, signer=SN30_OWNER_HOTKEY) == 0
    assert pinned.published == [(30, "endure.burn_bps=9800")]

    # Validators would block on an unpinned owner, so its rate is never published.
    rotated = FakeChain(genesis=MAINNET_GENESIS_HASH)
    assert _run(rotated) == 0
    assert "is not the pinned" in capsys.readouterr().err
    assert _run(rotated, *PUBLISH) == 1
    assert rotated.published == []


def test_mainnet_publishing_refuses_any_netuid_but_sn30() -> None:
    chain = FakeChain(genesis=MAINNET_GENESIS_HASH)
    chain.owner = SN30_OWNER_HOTKEY

    assert _run(chain, *PUBLISH, signer=SN30_OWNER_HOTKEY, netuid=31) == 1
    assert chain.published == []


def test_owner_and_commitment_are_read_at_one_block() -> None:
    chain = FakeChain("endure.burn_bps=9800")
    assert _run(chain) == 0

    # The head moves, but both reads stay pinned to the block first observed.
    [snapshot_block, commitment_block] = chain.read_blocks
    assert snapshot_block == commitment_block == 501
