"""Risk consensus publication shape (risk scope spec §Consensus, §Derived risk tier)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from endure.assessment.coordinates import (
    AssessmentConsensusRow,
    AssessmentCoordinate,
    AssessmentRealizedTarget,
)
from endure.assessment.registry import UniverseSnapshot
from endure.assessment.schemas.subnet_alpha_risk import (
    HORIZON_5D_SECONDS,
    HORIZON_30D_SECONDS,
    RISK_SCHEMA_ID,
    RiskOutput,
)
from endure.protocol.canonical import canonical_bundle_bytes
from endure.protocol.round_engine import DEFAULT_OFFSETS, compute_windows
from endure.publication.risk_feed import build_signed_risk_feed
from endure.scoring.assessment_orchestrator import (
    REALIZED_TARGET_RESOLVED,
    REALIZED_TARGET_VOIDED,
)
from endure.storage.repository import Storage

OLDER_ROUND = "2026-06-08"
NEWER_ROUND = "2026-06-09"
ROUND = NEWER_ROUND
NOW = datetime(2026, 6, 9, 21, 0, tzinfo=UTC).isoformat()


def _open_risk_round(
    storage: Storage, round_id: str = ROUND, tickers: tuple[str, ...] = ("8",)
) -> None:
    storage.open_round(
        windows=compute_windows(date.fromisoformat(round_id), offsets=DEFAULT_OFFSETS),
        schema_id=RISK_SCHEMA_ID,
        universe=UniverseSnapshot(round_id=round_id, tickers=tickers, source_hash="h"),
        now_iso=NOW,
    )


def _row(
    netuid: int, horizon: int, output: RiskOutput, value: int
) -> AssessmentConsensusRow:
    return AssessmentConsensusRow(
        coordinate=AssessmentCoordinate.subnet_asset(
            netuid=netuid,
            horizon_seconds=horizon,
            output=output.value,
        ),
        value=Decimal(value),
        dispersion=Decimal(10),
        n_submitters=3,
    )


def _target(
    netuid: int, output: RiskOutput, value: int | None, status: str
) -> AssessmentRealizedTarget:
    return AssessmentRealizedTarget(
        coordinate=AssessmentCoordinate.subnet_asset(
            netuid=netuid,
            horizon_seconds=HORIZON_30D_SECONDS,
            output=output.value,
        ),
        value=None if value is None else Decimal(value),
        status=status,
    )


def _resolved_pair(
    netuid: int, drawdown: int, volatility: int
) -> list[AssessmentRealizedTarget]:
    return [
        _target(netuid, RiskOutput.MAX_DRAWDOWN, drawdown, REALIZED_TARGET_RESOLVED),
        _target(
            netuid,
            RiskOutput.REALIZED_VOLATILITY,
            volatility,
            REALIZED_TARGET_RESOLVED,
        ),
    ]


def test_risk_feed_shape_round_trips_canonically(migrated_storage: Storage) -> None:
    storage = migrated_storage
    _open_risk_round(storage)
    storage.publish_assessment_consensus_and_reveal(
        ROUND,
        RISK_SCHEMA_ID,
        [
            _row(8, HORIZON_5D_SECONDS, RiskOutput.MAX_DRAWDOWN, 700),
            _row(8, HORIZON_30D_SECONDS, RiskOutput.MAX_DRAWDOWN, 999),
            _row(8, HORIZON_30D_SECONDS, RiskOutput.REALIZED_VOLATILITY, 4999),
        ],
        now_iso=NOW,
    )
    storage.record_assessment_realized_targets(
        ROUND,
        RISK_SCHEMA_ID,
        [
            AssessmentRealizedTarget(
                coordinate=AssessmentCoordinate.subnet_asset(
                    netuid=8,
                    horizon_seconds=HORIZON_30D_SECONDS,
                    output=RiskOutput.MAX_DRAWDOWN.value,
                ),
                value=Decimal(900),
                status=REALIZED_TARGET_RESOLVED,
            ),
            AssessmentRealizedTarget(
                coordinate=AssessmentCoordinate.subnet_asset(
                    netuid=8,
                    horizon_seconds=HORIZON_30D_SECONDS,
                    output=RiskOutput.REALIZED_VOLATILITY.value,
                ),
                value=Decimal(4500),
                status=REALIZED_TARGET_RESOLVED,
            ),
        ],
        now_iso=NOW,
    )

    signed = build_signed_risk_feed(
        storage,
        signer=lambda payload: b"sig:" + payload[:4],
        signed_by="validator-hotkey",
    )

    assert signed["signature"]["signed_by"] == "validator-hotkey"
    assert signed["signature"]["signature_hex"] == "7369673a7b226173"
    assert canonical_bundle_bytes(signed["payload"]).startswith(b'{"as_of"')
    assert signed["payload"]["feed_schema_version"] == 1
    [subnet] = signed["payload"]["subnets"]
    assert subnet["netuid"] == 8
    assert subnet["tier"] == "A"
    assert subnet["tier_round_id"] == ROUND
    assert subnet["tier_as_of"] is not None
    assert subnet["consensus"][0]["median"] == "700"
    assert "tier_round_id" not in signed["payload"]
    assert "tier_as_of" not in signed["payload"]


def test_risk_feed_is_unrated_without_resolved_thirty_day_targets(
    migrated_storage: Storage,
) -> None:
    storage = migrated_storage
    _open_risk_round(storage)
    storage.publish_assessment_consensus_and_reveal(
        ROUND,
        RISK_SCHEMA_ID,
        [
            _row(8, HORIZON_30D_SECONDS, RiskOutput.MAX_DRAWDOWN, 999),
            _row(8, HORIZON_30D_SECONDS, RiskOutput.REALIZED_VOLATILITY, 4999),
        ],
        now_iso=NOW,
    )

    signed = build_signed_risk_feed(storage)

    [subnet] = signed["payload"]["subnets"]
    assert subnet["tier"] == "unrated"
    assert subnet["tier_round_id"] is None
    assert subnet["tier_as_of"] is None


def test_risk_feed_uses_per_netuid_newest_complete_resolved_round_for_tier(
    migrated_storage: Storage,
) -> None:
    storage = migrated_storage
    _open_risk_round(storage, OLDER_ROUND, ("8", "44"))
    _open_risk_round(storage, NEWER_ROUND, ("8", "44"))
    storage.publish_assessment_consensus_and_reveal(
        OLDER_ROUND,
        RISK_SCHEMA_ID,
        [
            _row(8, HORIZON_30D_SECONDS, RiskOutput.MAX_DRAWDOWN, 999),
            _row(8, HORIZON_30D_SECONDS, RiskOutput.REALIZED_VOLATILITY, 4999),
        ],
        now_iso=NOW,
    )
    storage.record_assessment_realized_targets(
        OLDER_ROUND,
        RISK_SCHEMA_ID,
        _resolved_pair(8, 900, 4500),
        now_iso=NOW,
    )
    storage.publish_assessment_consensus_and_reveal(
        NEWER_ROUND,
        RISK_SCHEMA_ID,
        [
            _row(8, HORIZON_30D_SECONDS, RiskOutput.MAX_DRAWDOWN, 3500),
            _row(8, HORIZON_30D_SECONDS, RiskOutput.REALIZED_VOLATILITY, 12000),
            _row(44, HORIZON_30D_SECONDS, RiskOutput.MAX_DRAWDOWN, 900),
            _row(44, HORIZON_30D_SECONDS, RiskOutput.REALIZED_VOLATILITY, 4500),
        ],
        now_iso=NOW,
    )
    storage.record_assessment_realized_targets(
        NEWER_ROUND,
        RISK_SCHEMA_ID,
        [
            _target(8, RiskOutput.MAX_DRAWDOWN, 3200, REALIZED_TARGET_RESOLVED),
            _target(8, RiskOutput.REALIZED_VOLATILITY, None, REALIZED_TARGET_VOIDED),
            *_resolved_pair(44, 900, 4500),
        ],
        now_iso=NOW,
    )

    signed = build_signed_risk_feed(storage)

    payload = signed["payload"]
    subnet_by_netuid = {subnet["netuid"]: subnet for subnet in payload["subnets"]}
    assert subnet_by_netuid[8]["tier"] == "A"
    assert subnet_by_netuid[8]["tier_round_id"] == OLDER_ROUND
    assert subnet_by_netuid[8]["consensus"][0]["median"] == "3500"
    assert subnet_by_netuid[44]["tier"] == "A"
    assert subnet_by_netuid[44]["tier_round_id"] == NEWER_ROUND
    assert payload["round_id"] == NEWER_ROUND
    assert "tier_round_id" not in payload
    assert "tier_as_of" not in payload


def test_risk_feed_falls_back_when_newest_resolved_round_has_no_consensus(
    migrated_storage: Storage,
) -> None:
    storage = migrated_storage
    _open_risk_round(storage, OLDER_ROUND, ("8",))
    _open_risk_round(storage, NEWER_ROUND, ("8",))
    storage.publish_assessment_consensus_and_reveal(
        OLDER_ROUND,
        RISK_SCHEMA_ID,
        [
            _row(8, HORIZON_30D_SECONDS, RiskOutput.MAX_DRAWDOWN, 999),
            _row(8, HORIZON_30D_SECONDS, RiskOutput.REALIZED_VOLATILITY, 4999),
        ],
        now_iso=NOW,
    )
    storage.record_assessment_realized_targets(
        OLDER_ROUND,
        RISK_SCHEMA_ID,
        _resolved_pair(8, 900, 4500),
        now_iso=NOW,
    )
    storage.publish_assessment_consensus_and_reveal(
        NEWER_ROUND,
        RISK_SCHEMA_ID,
        [],
        now_iso=NOW,
    )
    storage.record_assessment_realized_targets(
        NEWER_ROUND,
        RISK_SCHEMA_ID,
        _resolved_pair(8, 900, 4500),
        now_iso=NOW,
    )

    signed = build_signed_risk_feed(storage)

    [subnet] = signed["payload"]["subnets"]
    assert subnet["tier"] == "A"
    assert subnet["tier_round_id"] == OLDER_ROUND


def test_risk_feed_includes_tiered_netuid_absent_from_newest_consensus_universe(
    migrated_storage: Storage,
) -> None:
    storage = migrated_storage
    _open_risk_round(storage, OLDER_ROUND, ("8",))
    _open_risk_round(storage, NEWER_ROUND, ("44",))
    storage.publish_assessment_consensus_and_reveal(
        OLDER_ROUND,
        RISK_SCHEMA_ID,
        [
            _row(8, HORIZON_30D_SECONDS, RiskOutput.MAX_DRAWDOWN, 999),
            _row(8, HORIZON_30D_SECONDS, RiskOutput.REALIZED_VOLATILITY, 4999),
        ],
        now_iso=NOW,
    )
    storage.record_assessment_realized_targets(
        OLDER_ROUND,
        RISK_SCHEMA_ID,
        _resolved_pair(8, 900, 4500),
        now_iso=NOW,
    )
    storage.publish_assessment_consensus_and_reveal(
        NEWER_ROUND,
        RISK_SCHEMA_ID,
        [_row(44, HORIZON_30D_SECONDS, RiskOutput.MAX_DRAWDOWN, 1500)],
        now_iso=NOW,
    )

    signed = build_signed_risk_feed(storage)

    subnet_by_netuid = {
        subnet["netuid"]: subnet for subnet in signed["payload"]["subnets"]
    }
    assert list(subnet_by_netuid) == [8, 44]
    assert subnet_by_netuid[8]["tier"] == "A"
    assert subnet_by_netuid[8]["tier_round_id"] == OLDER_ROUND
    assert subnet_by_netuid[8]["consensus"] == []


def test_risk_feed_five_day_only_state_has_null_per_subnet_tier_fields(
    migrated_storage: Storage,
) -> None:
    storage = migrated_storage
    _open_risk_round(storage)
    storage.publish_assessment_consensus_and_reveal(
        ROUND,
        RISK_SCHEMA_ID,
        [_row(8, HORIZON_5D_SECONDS, RiskOutput.MAX_DRAWDOWN, 700)],
        now_iso=NOW,
    )

    signed = build_signed_risk_feed(storage)

    [subnet] = signed["payload"]["subnets"]
    assert subnet["tier"] == "unrated"
    assert subnet["tier_round_id"] is None
    assert subnet["tier_as_of"] is None


def test_risk_feed_without_rounds_lists_whitelist_unrated_with_null_tier_fields(
    migrated_storage: Storage,
) -> None:
    signed = build_signed_risk_feed(migrated_storage)

    payload = signed["payload"]
    assert payload["round_id"] is None
    assert {subnet["tier"] for subnet in payload["subnets"]} == {"unrated"}
    assert {subnet["tier_round_id"] for subnet in payload["subnets"]} == {None}
    assert {subnet["tier_as_of"] for subnet in payload["subnets"]} == {None}


def test_risk_feed_filters_consensus_and_tiers_to_current_whitelist(
    migrated_storage: Storage,
) -> None:
    storage = migrated_storage
    dewhitelisted_netuid = 999
    _open_risk_round(storage, ROUND, (str(dewhitelisted_netuid),))
    storage.publish_assessment_consensus_and_reveal(
        ROUND,
        RISK_SCHEMA_ID,
        [
            _row(
                dewhitelisted_netuid,
                HORIZON_30D_SECONDS,
                RiskOutput.MAX_DRAWDOWN,
                999,
            ),
            _row(
                dewhitelisted_netuid,
                HORIZON_30D_SECONDS,
                RiskOutput.REALIZED_VOLATILITY,
                4999,
            ),
        ],
        now_iso=NOW,
    )
    storage.record_assessment_realized_targets(
        ROUND,
        RISK_SCHEMA_ID,
        _resolved_pair(dewhitelisted_netuid, 900, 4500),
        now_iso=NOW,
    )

    signed = build_signed_risk_feed(storage)

    assert dewhitelisted_netuid not in {
        subnet["netuid"] for subnet in signed["payload"]["subnets"]
    }
