from __future__ import annotations

import hashlib
from collections.abc import Callable
from decimal import Decimal

import pytest

from endure.scoring.lending.market_data import (
    AlphaMarketDataError,
    AlphaPriceSeries,
    AlphaPriceSnapshot,
    FixtureAlphaPriceProvider,
    ResolutionWindow,
    canonical_alpha_price_payload_bytes,
    collateral_factor_target_for_netuid,
    parse_alpha_price_payload,
    recorded_mainnet_fixture_provider,
)
from endure.scoring.market_data import alpha_snapshot_from_reserves


def _snapshots() -> tuple[AlphaPriceSnapshot, ...]:
    return (
        AlphaPriceSnapshot(
            netuid=44,
            block=10,
            price_tao_per_alpha=Decimal("1.00"),
            tao_reserve_rao=1_000,
        ),
        AlphaPriceSnapshot(
            netuid=44,
            block=20,
            price_tao_per_alpha=Decimal("0.70"),
            tao_reserve_rao=900,
        ),
        AlphaPriceSnapshot(
            netuid=44,
            block=30,
            price_tao_per_alpha=Decimal("0.40"),
            tao_reserve_rao=800,
        ),
        AlphaPriceSnapshot(
            netuid=44,
            block=40,
            price_tao_per_alpha=Decimal("0.55"),
            tao_reserve_rao=850,
        ),
    )


def _series() -> AlphaPriceSeries:
    return AlphaPriceSeries(source="fixture", netuid=44, snapshots=_snapshots())


def _full_window() -> ResolutionWindow:
    return ResolutionWindow(start_block=0, horizon_blocks=2**63 - 1)


class TestAlphaMarketData:
    def test_recorded_netuid_44_drawdown_derives_collateral_factor(self) -> None:
        provider = recorded_mainnet_fixture_provider()

        target = collateral_factor_target_for_netuid(
            provider, netuid=44, window=_full_window()
        )

        assert target == 9375

    def test_recorded_netuid_8_drawdown_derives_collateral_factor(self) -> None:
        provider = recorded_mainnet_fixture_provider()

        target = collateral_factor_target_for_netuid(
            provider, netuid=8, window=_full_window()
        )

        assert target == 8599

    def test_missing_target_is_skipped(self) -> None:
        provider = recorded_mainnet_fixture_provider()

        assert (
            collateral_factor_target_for_netuid(
                provider, netuid=288, window=_full_window()
            )
            is None
        )

    def test_single_snapshot_target_is_skipped_before_collateral_factor_math(
        self,
    ) -> None:
        provider = FixtureAlphaPriceProvider(
            series_by_netuid={
                44: AlphaPriceSeries(
                    source="single", netuid=44, snapshots=_snapshots()[:1]
                )
            }
        )

        assert (
            collateral_factor_target_for_netuid(
                provider, netuid=44, window=_full_window()
            )
            is None
        )

    def test_wrong_netuid_series_from_provider_is_rejected(self) -> None:
        provider = FixtureAlphaPriceProvider(series_by_netuid={8: _series()})

        with pytest.raises(AlphaMarketDataError, match="expected 8"):
            collateral_factor_target_for_netuid(
                provider, netuid=8, window=_full_window()
            )

    def test_negative_liquidation_buffer_surfaces_instead_of_skipping(self) -> None:
        provider = recorded_mainnet_fixture_provider()

        with pytest.raises(ValueError, match="non-negative"):
            collateral_factor_target_for_netuid(
                provider,
                netuid=44,
                liquidation_buffer_bps=-1,
                window=_full_window(),
            )

    def test_payload_hash_is_deterministic(self) -> None:
        assert _series().payload_hash == _series().payload_hash

    def test_payload_hash_matches_exact_payload_bytes(self) -> None:
        payload = canonical_alpha_price_payload_bytes(
            source="fixture",
            netuid=44,
            snapshots=_snapshots(),
        )

        parsed = parse_alpha_price_payload(payload)

        assert parsed.payload_hash == hashlib.sha256(payload).hexdigest()

    def test_parse_canonical_payload_round_trips(self) -> None:
        payload = canonical_alpha_price_payload_bytes(
            source="fixture",
            netuid=44,
            snapshots=_snapshots(),
        )

        parsed = parse_alpha_price_payload(payload)

        assert parsed.netuid == 44
        assert parsed.prices == (
            Decimal("1.00"),
            Decimal("0.70"),
            Decimal("0.40"),
            Decimal("0.55"),
        )
        assert tuple(snapshot.tao_reserve_rao for snapshot in parsed.snapshots) == (
            1_000,
            900,
            800,
            850,
        )

    def test_payload_hash_changes_when_reserve_changes(self) -> None:
        original = _series().payload_hash
        changed = AlphaPriceSeries(
            source="fixture",
            netuid=44,
            snapshots=(
                *_snapshots()[:-1],
                AlphaPriceSnapshot(
                    netuid=44,
                    block=40,
                    price_tao_per_alpha=Decimal("0.55"),
                    tao_reserve_rao=851,
                ),
            ),
        ).payload_hash

        assert changed != original

    def test_parse_rejects_non_canonical_payload(self) -> None:
        canonical = canonical_alpha_price_payload_bytes(
            source="fixture",
            netuid=44,
            snapshots=_snapshots(),
        )
        with_whitespace = canonical.replace(b":", b": ")

        with pytest.raises(AlphaMarketDataError, match="canonical"):
            parse_alpha_price_payload(with_whitespace)

    def test_parse_rejects_non_canonical_price_string(self) -> None:
        payload = (
            b'{"netuid":44,"snapshots":[{"block":10,"price_tao_per_alpha":"+1e1",'
            b'"tao_reserve_rao":1},{"block":20,"price_tao_per_alpha":"1",'
            b'"tao_reserve_rao":1}],"source":"fixture"}'
        )

        with pytest.raises(AlphaMarketDataError, match="canonical"):
            parse_alpha_price_payload(payload)

    def test_parse_rejects_non_decimal_price(self) -> None:
        payload = (
            b'{"netuid":44,"snapshots":[{"block":10,'
            b'"price_tao_per_alpha":"bad","tao_reserve_rao":1}],"source":"fixture"}'
        )

        with pytest.raises(AlphaMarketDataError, match="decimal string"):
            parse_alpha_price_payload(payload)

    def test_parse_rejects_bool_integer_fields(self) -> None:
        payload = (
            b'{"netuid":true,"snapshots":[{"block":10,'
            b'"price_tao_per_alpha":"1","tao_reserve_rao":1}],"source":"fixture"}'
        )

        with pytest.raises(AlphaMarketDataError, match="must be an integer"):
            parse_alpha_price_payload(payload)

    def test_parse_rejects_oversized_integer_literals_as_market_data_error(
        self,
    ) -> None:
        payload = b'{"netuid":' + b"1" * 5000 + b',"snapshots":[],"source":"fixture"}'

        with pytest.raises(AlphaMarketDataError, match="valid JSON"):
            parse_alpha_price_payload(payload)

    def test_payload_byte_cap_is_enforced_before_json_parse(self) -> None:
        with pytest.raises(AlphaMarketDataError, match="too large"):
            parse_alpha_price_payload(b'{"source":"fixture"}', max_payload_bytes=1)

    def test_non_positive_price_is_rejected(self) -> None:
        with pytest.raises(AlphaMarketDataError, match="finite positive"):
            AlphaPriceSnapshot(
                netuid=44,
                block=10,
                price_tao_per_alpha=Decimal("0"),
                tao_reserve_rao=1,
            )

    def test_non_finite_price_is_rejected(self) -> None:
        with pytest.raises(AlphaMarketDataError, match="finite positive"):
            AlphaPriceSnapshot(
                netuid=44,
                block=10,
                price_tao_per_alpha=Decimal("Infinity"),
                tao_reserve_rao=1,
            )

    def test_negative_reserve_is_rejected(self) -> None:
        with pytest.raises(AlphaMarketDataError, match="reserve"):
            AlphaPriceSnapshot(
                netuid=44,
                block=10,
                price_tao_per_alpha=Decimal("1"),
                tao_reserve_rao=-1,
            )

    def test_snapshot_netuid_must_match_series(self) -> None:
        with pytest.raises(AlphaMarketDataError, match="does not match"):
            AlphaPriceSeries(
                source="fixture",
                netuid=44,
                snapshots=(
                    AlphaPriceSnapshot(
                        netuid=44,
                        block=10,
                        price_tao_per_alpha=Decimal("1"),
                        tao_reserve_rao=1,
                    ),
                    AlphaPriceSnapshot(
                        netuid=8,
                        block=20,
                        price_tao_per_alpha=Decimal("1"),
                        tao_reserve_rao=1,
                    ),
                ),
            )

    def test_blocks_must_be_ascending(self) -> None:
        with pytest.raises(AlphaMarketDataError, match="ascending"):
            AlphaPriceSeries(
                source="fixture",
                netuid=44,
                snapshots=(
                    AlphaPriceSnapshot(
                        netuid=44,
                        block=20,
                        price_tao_per_alpha=Decimal("1"),
                        tao_reserve_rao=1,
                    ),
                    AlphaPriceSnapshot(
                        netuid=44,
                        block=20,
                        price_tao_per_alpha=Decimal("1"),
                        tao_reserve_rao=1,
                    ),
                ),
            )


def _snap(*, netuid: int = 44, block: int = 1) -> AlphaPriceSnapshot:
    return AlphaPriceSnapshot(
        netuid=netuid,
        block=block,
        price_tao_per_alpha=Decimal("1"),
        tao_reserve_rao=1,
    )


def _ser(
    *,
    source: str = "fixture",
    netuid: int = 44,
    snapshots: tuple[AlphaPriceSnapshot, ...] | None = None,
) -> AlphaPriceSeries:
    return AlphaPriceSeries(
        source=source,
        netuid=netuid,
        snapshots=_snapshots() if snapshots is None else snapshots,
    )


def _snapshot_payload(*, block: str = "7", price: str = '"1"') -> bytes:
    return (
        '{"source":"s","netuid":44,"snapshots":[{"block":'
        + block
        + ',"price_tao_per_alpha":'
        + price
        + ',"tao_reserve_rao":1}]}'
    ).encode()


class TestAlphaMarketDataValidation:
    """Malformed market data is rejected at construction, never scored."""

    @pytest.mark.parametrize(
        ("build", "message"),
        [
            (lambda: ResolutionWindow(-1, 10), "start_block must be non-negative"),
            (lambda: ResolutionWindow(0, 0), "horizon_blocks must be positive"),
            (lambda: _snap(netuid=-1), "netuid must be non-negative"),
            (lambda: _snap(block=-1), "block must be non-negative"),
            (lambda: _ser(source=""), "source must be non-empty"),
            (lambda: _ser(netuid=-1), "netuid must be non-negative"),
            (lambda: _ser(snapshots=()), "at least one snapshot"),
        ],
    )
    def test_invalid_market_data_is_rejected(
        self, build: Callable[[], object], message: str
    ) -> None:
        with pytest.raises(AlphaMarketDataError, match=message):
            build()

    @pytest.mark.parametrize(
        ("tao_rao", "alpha_rao"),
        [(0, 1_000), (1_000, 0)],
        ids=["drained-tao", "drained-alpha"],
    )
    def test_drained_pool_reserves_cannot_produce_a_price(
        self, tao_rao: int, alpha_rao: int
    ) -> None:
        with pytest.raises(AlphaMarketDataError, match="empty reserves for netuid 44"):
            alpha_snapshot_from_reserves(
                netuid=44, block=7, tao_rao=tao_rao, alpha_rao=alpha_rao
            )

    def test_window_outside_every_snapshot_yields_no_series(self) -> None:
        # A resolution window that lands past the recorded history must read as
        # absent data, not as an empty-but-valid series.
        provider = FixtureAlphaPriceProvider(series_by_netuid={44: _series()})

        assert (
            provider.price_series(
                44, window=ResolutionWindow(start_block=10_000, horizon_blocks=10)
            )
            is None
        )

    def test_unknown_netuid_has_no_latest_observation(self) -> None:
        provider = FixtureAlphaPriceProvider(series_by_netuid={44: _series()})

        assert provider.latest_pool_observation(9_999) is None

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            (b"[]", "must be an object"),
            (b'{"source":"s","netuid":44,"snapshots":{}}', "snapshots must be a list"),
            (
                b'{"source":"s","netuid":44,"snapshots":[1]}',
                "snapshot must be an object",
            ),
            (b'{"source":"","netuid":44,"snapshots":[]}', "source must be a non-empty"),
            (
                b'{"source":"s","netuid":-1,"snapshots":[]}',
                "netuid must be non-negative",
            ),
            (_snapshot_payload(block='"7"'), "block must be an integer"),
            (_snapshot_payload(price="1"), "price_tao_per_alpha must be a decimal"),
            (
                _snapshot_payload(price='"nope"'),
                "price_tao_per_alpha must be a decimal",
            ),
        ],
    )
    def test_malformed_payload_is_rejected(self, payload: bytes, message: str) -> None:
        with pytest.raises(AlphaMarketDataError, match=message):
            parse_alpha_price_payload(payload)
