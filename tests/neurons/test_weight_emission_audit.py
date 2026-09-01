from __future__ import annotations

import hashlib
import inspect
import json
from decimal import Decimal
from unittest.mock import MagicMock

import bittensor as bt
import numpy as np
import pytest
from bittensor.core.types import ExtrinsicResponse
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError

import endure.storage.repository as storage_repository
from endure.assessment.schemas.subnet_alpha_risk import RISK_SCHEMA_ID
from endure.base.rate_gate import ChainRpcStalled, RateLimited
from endure.base.validator import (
    WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS,
    WEIGHT_EMISSION_PERIOD_BLOCKS,
    BaseValidatorNeuron,
    WeightEmissionAttempt,
)
from endure.protocol.validator_service import ValidatorRoundService
from endure.protocol.version_contract import CURRENT_VERSION_KEY
from endure.storage.repository import (
    Cr4RevealScanWindow,
    Storage,
    WeightCommitEvidence,
    WeightEmissionChainSnapshot,
    WeightEmissionRow,
    WeightRevealEvidence,
)
from endure.storage.tables import weight_emission_batches, weight_emission_rows
from neurons.validator import Validator

ZERO = Decimal("0")
CHAIN_IDENTITY = "genesis-a"
VALIDATOR_HOTKEY = "validator-hotkey"


def _intent_hash(
    *,
    chain_identity: str,
    netuid: int,
    validator_uid: int,
    validator_hotkey: str,
    targets: tuple[tuple[int, str, int], ...],
    protocol_version_key: int | None = None,
) -> str:
    identity = {
        "chain_identity": chain_identity,
        "netuid": netuid,
        "validator_uid": validator_uid,
        "validator_hotkey": validator_hotkey,
        "targets": sorted(targets),
    }
    if protocol_version_key is not None:
        identity["version_key"] = protocol_version_key
    payload = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    digest = hashlib.blake2b(payload, digest_size=32).hexdigest()
    if protocol_version_key is None:
        return digest
    return f"{protocol_version_key}:{digest}"


def _record_confirmation(
    storage: Storage,
    *,
    confirmation_state: str,
    status: str,
    submission_block: int,
    baseline_last_update_block: int,
    confirmation_deadline_block: int | None = None,
    cr4_reveal_deadline_block: int | None = None,
    rows: tuple[WeightEmissionRow, ...] = (),
    submission_mode: str = "direct",
    commitment_hash: str | None = None,
    reveal_round: int | None = None,
    validator_uid: int = 0,
    validator_hotkey: str = VALIDATOR_HOTKEY,
    with_deadline: bool = True,
    intent_protocol_version_key: int | None = CURRENT_VERSION_KEY,
    intent_hash_override: str | None = None,
) -> int:
    targets = tuple(
        (row.uid, row.miner_hotkey, row.weight_u16)
        for row in rows
        if row.weight_u16 is not None
    )
    effective_deadline = confirmation_deadline_block
    if (
        effective_deadline is None
        and with_deadline
        and confirmation_state in {"prepared", "submitted", "ambiguous"}
    ):
        effective_deadline = (
            submission_block
            + WEIGHT_EMISSION_PERIOD_BLOCKS
            + WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS
        )
    return storage.record_weight_emission(
        schema_id=RISK_SCHEMA_ID,
        round_id=None,
        emitted_at_iso="2026-08-09T00:00:00+00:00",
        block=submission_block,
        min_allowed_weights=1,
        max_weight_limit=Decimal("1"),
        metagraph_size=max(1, len(rows)),
        status=status,
        rows=rows,
        submission_block=submission_block,
        confirmation_state=confirmation_state,
        confirmation_deadline_block=effective_deadline,
        cr4_reveal_deadline_block=cr4_reveal_deadline_block,
        baseline_last_update_block=baseline_last_update_block,
        period_blocks=WEIGHT_EMISSION_PERIOD_BLOCKS,
        chain_identity=CHAIN_IDENTITY,
        netuid=1,
        validator_uid=validator_uid,
        validator_hotkey=validator_hotkey,
        submission_mode=submission_mode,
        intent_hash=(
            intent_hash_override
            if intent_hash_override is not None
            else _intent_hash(
                chain_identity=CHAIN_IDENTITY,
                netuid=1,
                validator_uid=validator_uid,
                validator_hotkey=validator_hotkey,
                targets=targets,
                protocol_version_key=intent_protocol_version_key,
            )
        ),
        protocol_version_key=intent_protocol_version_key,
        commitment_hash=commitment_hash,
        reveal_round=reveal_round,
    )


def _chain_snapshot(
    *,
    block: int,
    last_update_block: int,
    weights: tuple[tuple[int, int], ...] = (),
    commitments: tuple[WeightCommitEvidence, ...] = (),
    reveals: tuple[WeightRevealEvidence, ...] = (),
    validator_uid: int = 0,
    validator_hotkey: str = VALIDATOR_HOTKEY,
    hotkeys: tuple[tuple[int, str], ...] | None = None,
    reveal_scan_complete: bool = True,
) -> WeightEmissionChainSnapshot:
    known_hotkeys = {
        0: validator_hotkey,
        1: "hk-b",
        2: "hk-pad",
        3: "miner-a",
        4: "miner-a",
    }
    finalized_hotkeys = hotkeys
    if finalized_hotkeys is None:
        finalized_uids = sorted({validator_uid, *(uid for uid, _weight in weights)})
        finalized_hotkeys = tuple((uid, known_hotkeys[uid]) for uid in finalized_uids)
    return WeightEmissionChainSnapshot(
        chain_identity=CHAIN_IDENTITY,
        netuid=1,
        validator_uid=validator_uid,
        validator_hotkey=validator_hotkey,
        block=block,
        last_update_block=last_update_block,
        weights=weights,
        commitments=commitments,
        reveals=reveals,
        hotkeys=finalized_hotkeys,
        reveal_scan_complete=reveal_scan_complete,
    )


def _attempt(
    *,
    status: str = "submitted",
    hotkeys: tuple[str, ...] = (VALIDATOR_HOTKEY, "hk-b", "hk-pad"),
    raw_weights: tuple[Decimal, ...] = (
        Decimal("0.66"),
        Decimal("0.34"),
        ZERO,
    ),
    processed_uids: tuple[int, ...] = (0, 1, 2),
    processed_weights: tuple[Decimal, ...] = (
        Decimal("0.6"),
        Decimal("0.000001"),
        Decimal("0.399999"),
    ),
    uint_uids: tuple[int, ...] = (0, 2),
    uint_weights: tuple[int, ...] = (65535, 43689),
    min_allowed_weights: int | None = 1,
    max_weight_limit: Decimal | None = Decimal("0.9"),
    block: int | None = 123,
    submission_block: int | None = 122,
    baseline_last_update_block: int | None = 100,
    period_blocks: int | None = WEIGHT_EMISSION_PERIOD_BLOCKS,
    confirmation_state: str | None = "submitted",
    chain_identity: str | None = CHAIN_IDENTITY,
    netuid: int | None = 1,
    validator_uid: int | None = 0,
    validator_hotkey: str | None = VALIDATOR_HOTKEY,
    submission_mode: str | None = "direct",
    intent_hash: str | None = None,
    protocol_version_key: int | None = CURRENT_VERSION_KEY,
    commitment_hash: str | None = None,
    reveal_round: int | None = None,
    confirmation_deadline_block: int | None = None,
    cr4_reveal_deadline_block: int | None = None,
) -> WeightEmissionAttempt:
    vector = tuple(zip(uint_uids, uint_weights, strict=True))
    effective_deadline = confirmation_deadline_block
    if (
        effective_deadline is None
        and confirmation_state in {"prepared", "submitted", "ambiguous"}
        and submission_block is not None
        and period_blocks is not None
    ):
        effective_deadline = (
            submission_block + period_blocks + WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS
        )
    return WeightEmissionAttempt(
        hotkeys=hotkeys,
        raw_weights=raw_weights,
        processed_uids=processed_uids,
        processed_weights=processed_weights,
        uint_uids=uint_uids,
        uint_weights=uint_weights,
        min_allowed_weights=min_allowed_weights,
        max_weight_limit=max_weight_limit,
        status=status,
        block=block,
        submission_block=submission_block,
        baseline_last_update_block=baseline_last_update_block,
        period_blocks=period_blocks,
        confirmation_state=confirmation_state,
        chain_identity=chain_identity,
        netuid=netuid,
        validator_uid=validator_uid,
        validator_hotkey=validator_hotkey,
        submission_mode=submission_mode,
        intent_hash=(
            intent_hash
            if intent_hash is not None
            else _intent_hash(
                chain_identity=chain_identity or "",
                netuid=netuid or 0,
                validator_uid=validator_uid or 0,
                validator_hotkey=validator_hotkey or "",
                targets=tuple((uid, hotkeys[uid], weight) for uid, weight in vector),
                protocol_version_key=protocol_version_key,
            )
        ),
        protocol_version_key=protocol_version_key,
        commitment_hash=commitment_hash,
        reveal_round=reveal_round,
        confirmation_deadline_block=effective_deadline,
        cr4_reveal_deadline_block=cr4_reveal_deadline_block,
    )


def _audit_validator(storage: Storage) -> Validator:
    validator = Validator.__new__(Validator)
    validator._storage = storage
    validator._schema_id = RISK_SCHEMA_ID
    validator.config = MagicMock()
    validator.config.neuron.epoch_length = 60
    validator.config.netuid = 1
    validator._blended_snapshot = {
        VALIDATOR_HOTKEY: Decimal("0.5"),
        "hk-b": Decimal("0.25"),
    }
    return validator


def _configure_finalized_chain(
    validator: Validator,
    *,
    finalized_block: int,
    last_updates: tuple[int, ...],
    weights: tuple[tuple[int, tuple[tuple[int, int], ...]], ...] = (),
    commitments: tuple[tuple[str, int, str, int], ...] = (),
    hotkeys: tuple[tuple[int, str], ...] | None = None,
) -> None:
    gated = MagicMock()
    gated.finalized_block.return_value = finalized_block
    gated.last_updates_at.return_value = last_updates
    gated.weights_at.return_value = weights
    gated.timelocked_weight_commits_at.return_value = commitments
    gated.timelocked_weight_reveals_between.return_value = ()
    target_uids = {
        target_uid
        for _validator_uid, validator_weights in weights
        for target_uid, _weight in validator_weights
    }
    known_hotkeys = {
        0: VALIDATOR_HOTKEY,
        1: "validator-b",
        2: "hk-pad",
        3: "miner-a",
    }
    finalized_uids = set(range(len(last_updates))) | target_uids
    gated.hotkeys_at.return_value = hotkeys or tuple(
        (uid, known_hotkeys[uid]) for uid in sorted(finalized_uids)
    )
    gated.get_block_hash.return_value = CHAIN_IDENTITY
    validator.gated_subtensor = gated


def _recorded_emission(
    storage: Storage,
) -> tuple[dict[str, int | str | None], list[WeightEmissionRow]]:
    with storage._engine.connect() as connection:
        [batch] = connection.execute(
            select(weight_emission_batches).order_by(weight_emission_batches.c.id)
        ).all()
        rows = connection.execute(
            select(weight_emission_rows)
            .where(weight_emission_rows.c.batch_id == batch._mapping["id"])
            .order_by(weight_emission_rows.c.uid)
        ).all()
    return (
        dict(batch._mapping),
        [
            WeightEmissionRow(
                miner_hotkey=str(row._mapping["miner_hotkey"]),
                uid=int(row._mapping["uid"]),
                blended_score=(
                    None
                    if row._mapping["blended_score_text"] is None
                    else Decimal(str(row._mapping["blended_score_text"]))
                ),
                weight_norm_precap=(
                    None
                    if row._mapping["weight_norm_precap_text"] is None
                    else Decimal(str(row._mapping["weight_norm_precap_text"]))
                ),
                weight_processed=Decimal(str(row._mapping["weight_processed_text"])),
                weight_u16=(
                    None
                    if row._mapping["weight_u16"] is None
                    else int(row._mapping["weight_u16"])
                ),
                emitted=bool(row._mapping["emitted"]),
                confirmed=batch._mapping["confirmation_state"] == "confirmed",
            )
            for row in rows
        ],
    )


class TestWeightEmissionAudit:
    def test_open_confirmation_requires_baseline_and_period(
        self, storage: Storage
    ) -> None:
        with pytest.raises(ValueError, match="open confirmation requires"):
            storage.record_weight_emission(
                schema_id=RISK_SCHEMA_ID,
                round_id=None,
                emitted_at_iso="2026-08-09T00:00:00+00:00",
                block=101,
                min_allowed_weights=1,
                max_weight_limit=Decimal("1"),
                metagraph_size=1,
                status="error",
                rows=(),
                submission_block=100,
                confirmation_state="prepared",
            )

    def test_confirmation_state_must_match_transport_status(
        self, storage: Storage
    ) -> None:
        with pytest.raises(ValueError, match="does not match transport status"):
            _record_confirmation(
                storage,
                status="error",
                submission_block=100,
                confirmation_state="submitted",
                baseline_last_update_block=90,
            )

    def test_prepared_batch_transitions_only_once(self, storage: Storage) -> None:
        batch_id = _record_confirmation(
            storage,
            status="error",
            submission_block=100,
            confirmation_state="prepared",
            baseline_last_update_block=90,
        )

        with pytest.raises(ValueError, match="does not match transport status"):
            storage.transition_weight_emission_attempt(
                batch_id=batch_id,
                status="failed",
                confirmation_state="submitted",
                submission_mode="direct",
                commitment_hash=None,
                reveal_round=None,
                confirmation_deadline_block=240,
            )
        storage.transition_weight_emission_attempt(
            batch_id=batch_id,
            status="submitted",
            confirmation_state="submitted",
            submission_mode="direct",
            commitment_hash=None,
            reveal_round=None,
            confirmation_deadline_block=240,
        )

        with pytest.raises(RuntimeError, match="prepared weight emission"):
            storage.transition_weight_emission_attempt(
                batch_id=batch_id,
                status="failed",
                confirmation_state="failed",
                submission_mode="direct",
                commitment_hash=None,
                reveal_round=None,
                confirmation_deadline_block=None,
            )

    def test_transition_write_failure_leaves_prepared_batch_open(
        self, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        validator = _audit_validator(storage)
        prepared = _attempt(status="error", confirmation_state="prepared")
        batch_id = validator._on_weights_prepared(prepared)
        transition = MagicMock(side_effect=RuntimeError("database locked"))
        monkeypatch.setattr(storage, "transition_weight_emission_attempt", transition)

        with pytest.raises(RuntimeError, match="database locked"):
            validator._on_weights_emitted(_attempt(), batch_id)

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "prepared"
        assert storage.has_open_weight_emission_confirmation(schema_id=RISK_SCHEMA_ID)

    def test_cr4_reveal_deadline_survives_prepared_transition(
        self, storage: Storage
    ) -> None:
        validator = _audit_validator(storage)
        prepared = _attempt(
            status="error",
            confirmation_state="prepared",
            submission_mode="cr4",
            cr4_reveal_deadline_block=732,
        )
        batch_id = validator._on_weights_prepared(prepared)

        validator._on_weights_emitted(
            _attempt(
                submission_mode="cr4",
                commitment_hash="abcd",
                reveal_round=42,
                cr4_reveal_deadline_block=732,
            ),
            batch_id,
        )

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["cr4_reveal_deadline_block"] == 732

    def test_open_confirmation_requires_prebroadcast_deadline(
        self, storage: Storage
    ) -> None:
        with pytest.raises(ValueError, match="confirmation deadline"):
            _record_confirmation(
                storage,
                status="error",
                submission_block=100,
                confirmation_state="prepared",
                baseline_last_update_block=90,
                with_deadline=False,
            )

    def test_negative_submission_block_is_rejected(self, storage: Storage) -> None:
        with pytest.raises(ValueError, match="submission_block must be non-negative"):
            storage.record_weight_emission(
                schema_id=RISK_SCHEMA_ID,
                round_id=None,
                emitted_at_iso="2026-08-09T00:00:00+00:00",
                block=1,
                min_allowed_weights=1,
                max_weight_limit=Decimal("1"),
                metagraph_size=1,
                status="submitted",
                rows=(),
                submission_block=-1,
                confirmation_state="submitted",
                confirmation_deadline_block=60,
            )

    def test_submitted_attempt_records_batch_and_rows(self, storage: Storage) -> None:
        validator = _audit_validator(storage)

        validator._on_weights_emitted(_attempt())

        batch, rows = _recorded_emission(storage)
        assert batch["status"] == "submitted"
        assert batch["confirmation_state"] == "submitted"
        assert batch["submission_block"] == 122
        assert batch["confirmation_deadline_block"] == 262
        assert batch["schema_id"] == RISK_SCHEMA_ID
        assert batch["block"] == 123
        assert batch["metagraph_size"] == 3
        assert batch["min_allowed_weights"] == 1
        assert batch["max_weight_limit_text"] == "0.9"
        by_hotkey = {row.miner_hotkey: row for row in rows}
        assert set(by_hotkey) == {VALIDATOR_HOTKEY, "hk-b", "hk-pad"}

        emitted = by_hotkey[VALIDATOR_HOTKEY]
        assert emitted.emitted is False
        assert emitted.confirmed is False
        assert emitted.weight_u16 == 65535
        assert emitted.blended_score == Decimal("0.5")
        assert emitted.weight_norm_precap == Decimal("0.66")
        assert emitted.weight_processed == Decimal("0.6")

        dropped = by_hotkey["hk-b"]
        assert dropped.emitted is False
        assert dropped.weight_u16 is None
        assert dropped.blended_score == Decimal("0.25")
        assert dropped.weight_processed == Decimal("0.000001")

        padded = by_hotkey["hk-pad"]
        assert padded.blended_score is None
        assert padded.weight_norm_precap is None
        assert padded.weight_processed == Decimal("0.399999")
        assert padded.emitted is False

    def test_failed_attempt_with_vectors_marks_no_row_emitted(
        self, storage: Storage
    ) -> None:
        validator = _audit_validator(storage)

        validator._on_weights_emitted(
            _attempt(status="failed", confirmation_state="failed")
        )

        batch, rows = _recorded_emission(storage)
        assert batch["status"] == "failed"
        assert len(rows) == 3
        assert all(row.emitted is False for row in rows)
        by_hotkey = {row.miner_hotkey: row for row in rows}
        assert by_hotkey[VALIDATOR_HOTKEY].weight_u16 == 65535

    def test_error_attempt_with_partial_vectors_persists_rows_not_emitted(
        self, storage: Storage
    ) -> None:
        validator = _audit_validator(storage)

        validator._on_weights_emitted(
            _attempt(
                status="error",
                confirmation_state="ambiguous",
                uint_uids=(),
                uint_weights=(),
            )
        )

        batch, rows = _recorded_emission(storage)
        assert batch["status"] == "error"
        assert len(rows) == 3
        assert all(row.emitted is False for row in rows)
        assert all(row.weight_u16 is None for row in rows)

    def test_error_attempt_without_processed_vector_records_batch_only(
        self, storage: Storage
    ) -> None:
        validator = _audit_validator(storage)

        validator._on_weights_emitted(
            _attempt(
                status="error",
                confirmation_state="ambiguous",
                processed_uids=(),
                processed_weights=(),
                uint_uids=(),
                uint_weights=(),
                min_allowed_weights=None,
                max_weight_limit=None,
            )
        )

        batch, rows = _recorded_emission(storage)
        assert batch["status"] == "error"
        assert batch["min_allowed_weights"] is None
        assert batch["max_weight_limit_text"] is None
        assert rows == []

    def test_empty_snapshot_falls_back_to_service_blended_snapshot(
        self, storage: Storage
    ) -> None:
        validator = _audit_validator(storage)
        validator._blended_snapshot = {}
        service = MagicMock()
        service.blended_snapshot.return_value = {VALIDATOR_HOTKEY: Decimal("0.5")}
        validator._service = service

        validator._on_weights_emitted(_attempt())

        _batch, rows = _recorded_emission(storage)
        by_hotkey = {row.miner_hotkey: row for row in rows}
        assert by_hotkey[VALIDATOR_HOTKEY].blended_score == Decimal("0.5")
        assert by_hotkey["hk-b"].blended_score is None
        service.blended_snapshot.assert_called_once_with()

    def test_emission_rows_keep_raw_round_program_blends(
        self, storage: Storage
    ) -> None:
        validator = _audit_validator(storage)
        validator._blended_snapshot = {}
        service = ValidatorRoundService.__new__(ValidatorRoundService)
        program = MagicMock()
        program.weights.return_value = {VALIDATOR_HOTKEY: Decimal("0.9")}
        program.blended_scores.return_value = {VALIDATOR_HOTKEY: Decimal("0.3")}
        service._round_program = program
        validator._service = service

        validator._on_weights_emitted(_attempt())

        _batch, rows = _recorded_emission(storage)
        by_hotkey = {row.miner_hotkey: row for row in rows}
        assert by_hotkey[VALIDATOR_HOTKEY].blended_score == Decimal("0.3")
        program.blended_scores.assert_called_once_with()
        program.weights.assert_not_called()

    def test_snapshot_error_records_null_blended_scores(self, storage: Storage) -> None:
        validator = _audit_validator(storage)
        validator._blended_snapshot = {}
        service = MagicMock()
        service.blended_snapshot.side_effect = RuntimeError("snapshot unavailable")
        validator._service = service

        validator._on_weights_emitted(_attempt())

        _batch, rows = _recorded_emission(storage)
        assert all(row.blended_score is None for row in rows)

    def test_unknown_status_rejected(self, storage: Storage) -> None:
        with pytest.raises(ValueError):
            storage.record_weight_emission(
                schema_id=RISK_SCHEMA_ID,
                round_id=None,
                emitted_at_iso="2026-07-22T00:00:00+00:00",
                block=None,
                min_allowed_weights=None,
                max_weight_limit=None,
                metagraph_size=0,
                status="finalized",
                rows=(),
            )

    def test_persisted_legacy_batch_confirms_after_later_metagraph_snapshot(
        self, storage: Storage
    ) -> None:
        batch_id = _record_confirmation(
            storage,
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=228,
            baseline_last_update_block=90,
            status="submitted",
        )
        legacy_hash = _intent_hash(
            chain_identity=CHAIN_IDENTITY,
            netuid=1,
            validator_uid=0,
            validator_hotkey=VALIDATOR_HOTKEY,
            targets=(),
        )
        with storage._engine.begin() as connection:
            connection.execute(
                update(weight_emission_batches)
                .where(weight_emission_batches.c.id == batch_id)
                .values(intent_hash=legacy_hash)
            )

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(block=101, last_update_block=101),
            finality_margin_blocks=0,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "confirmed"
        assert batch["confirmed_at"] == "2026-08-09T00:01:00+00:00"

    def test_current_versioned_batch_confirms(self, storage: Storage) -> None:
        _record_confirmation(
            storage,
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=228,
            baseline_last_update_block=90,
            status="submitted",
            intent_protocol_version_key=CURRENT_VERSION_KEY,
        )

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(block=101, last_update_block=101),
            finality_margin_blocks=0,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "confirmed"

    def test_has_confirmed_weight_emission_reports_any_confirmed_batch(
        self, storage: Storage
    ) -> None:
        assert not storage.has_confirmed_weight_emission(schema_id=RISK_SCHEMA_ID)

        _record_confirmation(
            storage,
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=228,
            baseline_last_update_block=90,
            status="submitted",
            intent_protocol_version_key=CURRENT_VERSION_KEY,
        )
        assert not storage.has_confirmed_weight_emission(schema_id=RISK_SCHEMA_ID)

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(block=101, last_update_block=101),
            finality_margin_blocks=0,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )
        assert storage.has_confirmed_weight_emission(schema_id=RISK_SCHEMA_ID)

    def test_new_open_emission_rejects_legacy_keyless_intent(
        self, storage: Storage
    ) -> None:
        with pytest.raises(
            ValueError, match="legacy weight intent cannot create an open emission"
        ):
            _record_confirmation(
                storage,
                submission_block=100,
                confirmation_state="submitted",
                confirmation_deadline_block=228,
                baseline_last_update_block=90,
                status="submitted",
                intent_protocol_version_key=None,
            )

    def test_malformed_versioned_intent_is_rejected(self, storage: Storage) -> None:
        with pytest.raises(ValueError):
            _record_confirmation(
                storage,
                submission_block=100,
                confirmation_state="submitted",
                confirmation_deadline_block=228,
                baseline_last_update_block=90,
                status="submitted",
                intent_hash_override="25:not-hex",
            )

    @pytest.mark.parametrize("wrong_key", [24, 1000])
    def test_new_versioned_emission_rejects_noncurrent_protocol_key(
        self, storage: Storage, wrong_key: int
    ) -> None:
        with pytest.raises(ValueError):
            _record_confirmation(
                storage,
                submission_block=100,
                confirmation_state="submitted",
                confirmation_deadline_block=228,
                baseline_last_update_block=90,
                status="submitted",
                intent_protocol_version_key=wrong_key,
            )

    def test_wrong_versioned_persisted_row_never_confirms_and_expires(
        self, storage: Storage
    ) -> None:
        batch_id = _record_confirmation(
            storage,
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=228,
            baseline_last_update_block=90,
            status="submitted",
            intent_protocol_version_key=CURRENT_VERSION_KEY,
        )
        wrong_hash = _intent_hash(
            chain_identity=CHAIN_IDENTITY,
            netuid=1,
            validator_uid=0,
            validator_hotkey=VALIDATOR_HOTKEY,
            targets=(),
            protocol_version_key=24,
        )
        with storage._engine.begin() as connection:
            connection.execute(
                update(weight_emission_batches)
                .where(weight_emission_batches.c.id == batch_id)
                .values(intent_hash=wrong_hash)
            )

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(block=101, last_update_block=101),
            finality_margin_blocks=0,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )
        [open_batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert open_batch["confirmation_state"] == "submitted"

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(block=229, last_update_block=101),
            finality_margin_blocks=0,
            confirmed_at_iso="2026-08-09T00:02:00+00:00",
        )
        [expired_batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert expired_batch["confirmation_state"] == "unconfirmed"

    def test_confirmation_protocol_key_cannot_be_overridden(
        self, storage: Storage
    ) -> None:
        batch_id = _record_confirmation(
            storage,
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=228,
            baseline_last_update_block=90,
            status="submitted",
            intent_protocol_version_key=CURRENT_VERSION_KEY,
        )
        wrong_hash = _intent_hash(
            chain_identity=CHAIN_IDENTITY,
            netuid=1,
            validator_uid=0,
            validator_hotkey=VALIDATOR_HOTKEY,
            targets=(),
            protocol_version_key=24,
        )
        with storage._engine.begin() as connection:
            connection.execute(
                update(weight_emission_batches)
                .where(weight_emission_batches.c.id == batch_id)
                .values(intent_hash=wrong_hash)
            )

        parameters = inspect.signature(
            storage.resolve_weight_emission_confirmations
        ).parameters
        assert "expected_protocol_version_key" not in parameters

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(block=101, last_update_block=101),
            finality_margin_blocks=0,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "submitted"

    def test_confirmation_runtime_key_is_patchable_only_at_trusted_boundary(
        self, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(storage_repository, "CURRENT_VERSION_KEY", 24)
        _record_confirmation(
            storage,
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=228,
            baseline_last_update_block=90,
            status="submitted",
            intent_protocol_version_key=24,
        )

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(block=101, last_update_block=101),
            finality_margin_blocks=0,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "confirmed"

    @pytest.mark.parametrize(
        "wrong_key",
        [24, 1000, None],
        ids=["wrong-24", "wrong-1000", "malformed"],
    )
    @pytest.mark.parametrize(
        ("initial_state", "status"),
        [("submitted", "submitted"), ("prepared", "error")],
    )
    def test_resumed_wrong_identity_never_confirms_and_expires(
        self,
        storage: Storage,
        wrong_key: int | None,
        initial_state: str,
        status: str,
    ) -> None:
        batch_id = _record_confirmation(
            storage,
            submission_block=100,
            confirmation_state=initial_state,
            confirmation_deadline_block=228,
            baseline_last_update_block=90,
            status=status,
            intent_protocol_version_key=CURRENT_VERSION_KEY,
        )
        wrong_hash = (
            "25:not-hex"
            if wrong_key is None
            else _intent_hash(
                chain_identity=CHAIN_IDENTITY,
                netuid=1,
                validator_uid=0,
                validator_hotkey=VALIDATOR_HOTKEY,
                targets=(),
                protocol_version_key=wrong_key,
            )
        )
        with storage._engine.begin() as connection:
            connection.execute(
                update(weight_emission_batches)
                .where(weight_emission_batches.c.id == batch_id)
                .values(intent_hash=wrong_hash)
            )
        resumed = Storage.from_url(storage._engine.url.render_as_string())

        resumed.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(block=101, last_update_block=101),
            finality_margin_blocks=0,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )
        [open_batch] = resumed.weight_emission_history(RISK_SCHEMA_ID)
        assert open_batch["confirmation_state"] == initial_state

        resumed.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(block=229, last_update_block=101),
            finality_margin_blocks=0,
            confirmed_at_iso="2026-08-09T00:02:00+00:00",
        )
        [expired_batch] = resumed.weight_emission_history(RISK_SCHEMA_ID)
        assert expired_batch["confirmation_state"] == "unconfirmed"

    def test_confirmation_marks_only_included_rows_as_emitted(
        self, storage: Storage
    ) -> None:
        validator = _audit_validator(storage)
        validator._on_weights_emitted(_attempt())

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=123,
                last_update_block=123,
                weights=((0, 65535), (2, 43689)),
            ),
            finality_margin_blocks=0,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )

        _batch, rows = _recorded_emission(storage)
        assert [row.emitted for row in rows] == [True, False, True]
        [history_batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        history_rows = history_batch["rows"]
        assert isinstance(history_rows, list)
        assert [row["confirmed"] for row in history_rows] == [True, False, True]
        assert [row["emitted"] for row in history_rows] == [True, False, True]

    def test_direct_confirmation_requires_exact_finalized_vector(
        self, storage: Storage
    ) -> None:
        row = WeightEmissionRow(
            miner_hotkey="miner-a",
            uid=3,
            blended_score=Decimal("1"),
            weight_norm_precap=Decimal("1"),
            weight_processed=Decimal("1"),
            weight_u16=65535,
            emitted=False,
        )
        _record_confirmation(
            storage,
            status="submitted",
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=240,
            baseline_last_update_block=90,
            rows=(row,),
        )

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=150,
                last_update_block=110,
                weights=((4, 65535),),
            ),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "submitted"

    def test_cr4_commit_stays_open_until_post_commit_update_applies_exact_vector(
        self, storage: Storage
    ) -> None:
        row = WeightEmissionRow(
            miner_hotkey="miner-a",
            uid=3,
            blended_score=Decimal("1"),
            weight_norm_precap=Decimal("1"),
            weight_processed=Decimal("1"),
            weight_u16=65535,
            emitted=False,
        )
        _record_confirmation(
            storage,
            status="submitted",
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=240,
            baseline_last_update_block=90,
            rows=(row,),
            submission_mode="cr4",
            commitment_hash="abcd",
            reveal_round=42,
            intent_protocol_version_key=CURRENT_VERSION_KEY,
        )

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=120,
                last_update_block=110,
                weights=((3, 65535),),
                commitments=(
                    WeightCommitEvidence(
                        validator_hotkey=VALIDATOR_HOTKEY,
                        commit_block=108,
                        commitment_hash="abcd",
                        reveal_round=42,
                    ),
                ),
            ),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )
        [committed] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert committed["confirmation_state"] == "submitted"
        assert committed["commitment_observed_block"] == 108
        assert committed["commitment_observed_last_update_block"] == 110

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=180,
                last_update_block=110,
                weights=((3, 65535),),
            ),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:02:00+00:00",
        )

        [revealed] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert revealed["confirmation_state"] == "submitted"
        assert revealed["confirmed_at"] is None

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=241,
                last_update_block=110,
                weights=((3, 65535),),
            ),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:03:00+00:00",
        )

        [expired] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert expired["confirmation_state"] == "unconfirmed"

    def test_cr4_matching_preexisting_vector_never_confirms_from_commit_update(
        self, storage: Storage
    ) -> None:
        row = WeightEmissionRow(
            miner_hotkey="miner-a",
            uid=3,
            blended_score=Decimal("1"),
            weight_norm_precap=Decimal("1"),
            weight_processed=Decimal("1"),
            weight_u16=65535,
            emitted=False,
        )
        _record_confirmation(
            storage,
            status="submitted",
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=240,
            baseline_last_update_block=90,
            rows=(row,),
            submission_mode="cr4",
            commitment_hash="abcd",
            reveal_round=42,
        )

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=200,
                last_update_block=108,
                weights=((3, 65535),),
            ),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:02:00+00:00",
        )

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "submitted"

    def test_cr4_reveal_history_scan_starts_before_deadline(
        self, storage: Storage
    ) -> None:
        batch_id = _record_confirmation(
            storage,
            status="submitted",
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=240,
            cr4_reveal_deadline_block=300,
            baseline_last_update_block=90,
            submission_mode="cr4",
            commitment_hash="abcd",
            reveal_round=42,
        )

        assert storage.cr4_reveal_scan_window(
            schema_id=RISK_SCHEMA_ID, finalized_block=299
        ) == Cr4RevealScanWindow(batch_id, 100, 131, False)
        storage.advance_cr4_reveal_scan_cursor(batch_id=batch_id, scanned_through=131)

        assert storage.cr4_reveal_scan_window(
            schema_id=RISK_SCHEMA_ID, finalized_block=350
        ) == Cr4RevealScanWindow(batch_id, 132, 163, False)
        storage.advance_cr4_reveal_scan_cursor(batch_id=batch_id, scanned_through=268)

        assert storage.cr4_reveal_scan_window(
            schema_id=RISK_SCHEMA_ID, finalized_block=350
        ) == Cr4RevealScanWindow(batch_id, 269, 300, True)
        storage.advance_cr4_reveal_scan_cursor(batch_id=batch_id, scanned_through=300)

        assert (
            storage.cr4_reveal_scan_window(
                schema_id=RISK_SCHEMA_ID, finalized_block=350
            )
            is None
        )

    def test_partial_cr4_reveal_scan_does_not_expire_open_batch(
        self, storage: Storage
    ) -> None:
        _record_confirmation(
            storage,
            status="submitted",
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=240,
            cr4_reveal_deadline_block=300,
            baseline_last_update_block=90,
            submission_mode="cr4",
            commitment_hash="abcd",
            reveal_round=42,
        )

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=350,
                last_update_block=108,
                reveal_scan_complete=False,
            ),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "submitted"

    def test_cr4_matching_reveal_identity_confirms_without_second_last_update(
        self, storage: Storage
    ) -> None:
        reveal = WeightRevealEvidence(
            validator_hotkey=VALIDATOR_HOTKEY,
            netuid=1,
            reveal_block=150,
        )
        _record_confirmation(
            storage,
            status="submitted",
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=240,
            baseline_last_update_block=90,
            rows=(
                WeightEmissionRow(
                    miner_hotkey="miner-a",
                    uid=3,
                    blended_score=Decimal("1"),
                    weight_norm_precap=Decimal("1"),
                    weight_processed=Decimal("1"),
                    weight_u16=65535,
                    emitted=False,
                ),
            ),
            submission_mode="cr4",
            commitment_hash="abcd",
            reveal_round=42,
        )

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=120,
                last_update_block=108,
                weights=((3, 65535),),
                commitments=(
                    WeightCommitEvidence(
                        validator_hotkey=VALIDATOR_HOTKEY,
                        commit_block=108,
                        commitment_hash="abcd",
                        reveal_round=42,
                    ),
                ),
            ),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )
        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=200,
                last_update_block=108,
                weights=((3, 65535),),
                reveals=(reveal,),
            ),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:02:00+00:00",
        )

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "confirmed"

    def test_cr4_tempo_360_commit_stays_open_until_reveal_window_expires(
        self, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commitment = WeightCommitEvidence(
            validator_hotkey=VALIDATOR_HOTKEY,
            commit_block=108,
            commitment_hash="abcd",
            reveal_round=42,
        )
        _record_confirmation(
            storage,
            status="submitted",
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=240,
            cr4_reveal_deadline_block=832,
            baseline_last_update_block=90,
            submission_mode="cr4",
            commitment_hash="abcd",
            reveal_round=42,
            intent_protocol_version_key=CURRENT_VERSION_KEY,
        )
        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=120,
                last_update_block=110,
                commitments=(commitment,),
            ),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=300,
                last_update_block=110,
                commitments=(commitment,),
            ),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:02:00+00:00",
        )

        [pending] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert pending["confirmation_state"] == "submitted"
        health = storage.weight_emission_confirmation_health(
            schema_id=RISK_SCHEMA_ID, current_block=300
        )
        assert health.oldest_open_deadline_block == 832

        validator = _audit_validator(storage)
        validator.config.runtime.mode = "mock"
        validator.scores = [Decimal("1")]
        emit = MagicMock()
        monkeypatch.setattr(BaseValidatorNeuron, "set_weights", emit)
        validator.set_weights()
        emit.assert_not_called()

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=833,
                last_update_block=110,
                commitments=(commitment,),
            ),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:03:00+00:00",
        )
        [still_pending] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert still_pending["confirmation_state"] == "submitted"

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(block=834, last_update_block=110),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:04:00+00:00",
        )
        [expired] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert expired["confirmation_state"] == "unconfirmed"

    def test_legacy_cr4_observed_commit_stays_open_while_commitment_is_visible(
        self, storage: Storage
    ) -> None:
        commitment = WeightCommitEvidence(
            validator_hotkey=VALIDATOR_HOTKEY,
            commit_block=108,
            commitment_hash="abcd",
            reveal_round=42,
        )
        _record_confirmation(
            storage,
            status="submitted",
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=240,
            baseline_last_update_block=90,
            submission_mode="cr4",
            commitment_hash="abcd",
            reveal_round=42,
            intent_protocol_version_key=CURRENT_VERSION_KEY,
        )
        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=120,
                last_update_block=110,
                commitments=(commitment,),
            ),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=241,
                last_update_block=110,
                commitments=(commitment,),
            ),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:02:00+00:00",
        )

        [pending] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert pending["confirmation_state"] == "submitted"

    def test_cr4_application_after_direct_mortality_deadline_confirms(
        self, storage: Storage
    ) -> None:
        row = WeightEmissionRow(
            miner_hotkey="miner-a",
            uid=3,
            blended_score=Decimal("1"),
            weight_norm_precap=Decimal("1"),
            weight_processed=Decimal("1"),
            weight_u16=65535,
            emitted=False,
        )
        _record_confirmation(
            storage,
            status="submitted",
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=240,
            cr4_reveal_deadline_block=832,
            baseline_last_update_block=90,
            rows=(row,),
            submission_mode="cr4",
            commitment_hash="abcd",
            reveal_round=42,
        )
        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=120,
                last_update_block=110,
                commitments=(
                    WeightCommitEvidence(
                        validator_hotkey=VALIDATOR_HOTKEY,
                        commit_block=108,
                        commitment_hash="abcd",
                        reveal_round=42,
                    ),
                ),
            ),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=500,
                last_update_block=490,
                weights=((3, 65535),),
                reveals=(
                    WeightRevealEvidence(
                        validator_hotkey=VALIDATOR_HOTKEY,
                        netuid=1,
                        reveal_block=450,
                    ),
                ),
            ),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:02:00+00:00",
        )

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "confirmed"

    def test_cr4_commitment_uses_extended_postcall_inclusion_window(
        self, storage: Storage
    ) -> None:
        _record_confirmation(
            storage,
            status="submitted",
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=242,
            baseline_last_update_block=90,
            submission_mode="cr4",
            commitment_hash="abcd",
            reveal_round=42,
        )

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=230,
                last_update_block=90,
                commitments=(
                    WeightCommitEvidence(
                        validator_hotkey=VALIDATOR_HOTKEY,
                        commit_block=229,
                        commitment_hash="abcd",
                        reveal_round=42,
                    ),
                ),
            ),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "submitted"
        assert batch["commitment_observed_block"] == 229

    def test_cr4_missed_commitment_visibility_confirms_exact_applied_vector(
        self, storage: Storage
    ) -> None:
        row = WeightEmissionRow(
            miner_hotkey="miner-a",
            uid=3,
            blended_score=Decimal("1"),
            weight_norm_precap=Decimal("1"),
            weight_processed=Decimal("1"),
            weight_u16=65535,
            emitted=False,
        )
        _record_confirmation(
            storage,
            status="submitted",
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=240,
            baseline_last_update_block=90,
            rows=(row,),
            submission_mode="cr4",
            commitment_hash="abcd",
            reveal_round=42,
        )

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=300,
                last_update_block=290,
                weights=((3, 65535),),
                reveals=(
                    WeightRevealEvidence(
                        validator_hotkey=VALIDATOR_HOTKEY,
                        netuid=1,
                        reveal_block=220,
                    ),
                ),
            ),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:02:00+00:00",
        )

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "confirmed"

    def test_cr4_without_response_metadata_expires_when_no_commit_appears(
        self, storage: Storage
    ) -> None:
        _record_confirmation(
            storage,
            status="error",
            submission_block=100,
            confirmation_state="ambiguous",
            confirmation_deadline_block=240,
            baseline_last_update_block=90,
            submission_mode="cr4",
        )

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(block=241, last_update_block=90),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "unconfirmed"

    def test_cr4_without_response_metadata_recovers_unique_commitment(
        self, storage: Storage
    ) -> None:
        row = WeightEmissionRow(
            miner_hotkey="miner-a",
            uid=3,
            blended_score=Decimal("1"),
            weight_norm_precap=Decimal("1"),
            weight_processed=Decimal("1"),
            weight_u16=65535,
            emitted=False,
        )
        _record_confirmation(
            storage,
            status="error",
            submission_block=100,
            confirmation_state="ambiguous",
            confirmation_deadline_block=240,
            baseline_last_update_block=90,
            rows=(row,),
            submission_mode="cr4",
        )
        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=120,
                last_update_block=90,
                commitments=(
                    WeightCommitEvidence(
                        validator_hotkey=VALIDATOR_HOTKEY,
                        commit_block=108,
                        commitment_hash="recovered",
                        reveal_round=43,
                    ),
                ),
            ),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )

        [observed] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert observed["commitment_hash"] == "recovered"
        assert observed["reveal_round"] == 43

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=300,
                last_update_block=290,
                weights=((3, 65535),),
                reveals=(
                    WeightRevealEvidence(
                        validator_hotkey=VALIDATOR_HOTKEY,
                        netuid=1,
                        reveal_block=220,
                    ),
                ),
            ),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:02:00+00:00",
        )

        [confirmed] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert confirmed["confirmation_state"] == "confirmed"

    def test_cr4_without_response_metadata_rejects_multiple_commitments(
        self, storage: Storage
    ) -> None:
        row = WeightEmissionRow(
            miner_hotkey="miner-a",
            uid=3,
            blended_score=Decimal("1"),
            weight_norm_precap=Decimal("1"),
            weight_processed=Decimal("1"),
            weight_u16=65535,
            emitted=False,
        )
        _record_confirmation(
            storage,
            status="error",
            submission_block=100,
            confirmation_state="ambiguous",
            confirmation_deadline_block=240,
            baseline_last_update_block=90,
            rows=(row,),
            submission_mode="cr4",
        )

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=120,
                last_update_block=110,
                weights=((3, 65535),),
                commitments=(
                    WeightCommitEvidence(
                        validator_hotkey=VALIDATOR_HOTKEY,
                        commit_block=108,
                        commitment_hash="first",
                        reveal_round=43,
                    ),
                    WeightCommitEvidence(
                        validator_hotkey=VALIDATOR_HOTKEY,
                        commit_block=109,
                        commitment_hash="second",
                        reveal_round=44,
                    ),
                ),
            ),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )

        [ambiguous] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert ambiguous["confirmation_state"] == "ambiguous"
        assert ambiguous["commitment_observed_block"] is None

    @pytest.mark.parametrize("commit_block", [99, 229])
    def test_cr4_ignores_commitment_outside_attempt_mortality(
        self, storage: Storage, commit_block: int
    ) -> None:
        _record_confirmation(
            storage,
            status="submitted",
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=240,
            baseline_last_update_block=90,
            submission_mode="cr4",
            commitment_hash="abcd",
            reveal_round=42,
        )

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=230,
                last_update_block=110,
                commitments=(
                    WeightCommitEvidence(
                        validator_hotkey=VALIDATOR_HOTKEY,
                        commit_block=commit_block,
                        commitment_hash="abcd",
                        reveal_round=42,
                    ),
                ),
            ),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["commitment_observed_block"] is None

    def test_storage_rejects_preconfirmed_batch_and_rows(
        self, storage: Storage
    ) -> None:
        row = WeightEmissionRow(
            miner_hotkey="miner-a",
            uid=3,
            blended_score=Decimal("1"),
            weight_norm_precap=Decimal("1"),
            weight_processed=Decimal("1"),
            weight_u16=65535,
            emitted=True,
            confirmed=True,
        )

        with pytest.raises(ValueError, match="pre-confirmed or emitted"):
            _record_confirmation(
                storage,
                status="submitted",
                submission_block=100,
                confirmation_state="submitted",
                confirmation_deadline_block=240,
                baseline_last_update_block=90,
                rows=(row,),
            )
        with pytest.raises(ValueError, match="only be created by resolution"):
            _record_confirmation(
                storage,
                status="submitted",
                submission_block=100,
                confirmation_state="confirmed",
                confirmation_deadline_block=240,
                baseline_last_update_block=90,
            )

    def test_database_rejects_confirmed_timestamp_without_confirmed_state(
        self, storage: Storage
    ) -> None:
        with pytest.raises(IntegrityError), storage._engine.begin() as connection:
            connection.execute(
                insert(weight_emission_batches).values(
                    schema_id=RISK_SCHEMA_ID,
                    round_id=None,
                    emitted_at="2026-08-09T00:00:00+00:00",
                    block=100,
                    min_allowed_weights=1,
                    max_weight_limit_text="1",
                    metagraph_size=1,
                    status="submitted",
                    confirmation_state=None,
                    confirmed_at="forged",
                )
            )

    def test_database_rejects_emitted_row_for_unconfirmed_batch(
        self, storage: Storage
    ) -> None:
        batch_id = _record_confirmation(
            storage,
            status="submitted",
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=240,
            baseline_last_update_block=90,
        )

        with pytest.raises(IntegrityError), storage._engine.begin() as connection:
            connection.execute(
                insert(weight_emission_rows).values(
                    batch_id=batch_id,
                    miner_hotkey="miner-a",
                    uid=3,
                    blended_score_text="1",
                    weight_norm_precap_text="1",
                    weight_processed_text="1",
                    weight_u16=65535,
                    emitted=True,
                )
            )

    def test_database_rejects_detaching_emitted_row_from_confirmed_batch(
        self, storage: Storage
    ) -> None:
        row = WeightEmissionRow(
            miner_hotkey="miner-a",
            uid=3,
            blended_score=Decimal("1"),
            weight_norm_precap=Decimal("1"),
            weight_processed=Decimal("1"),
            weight_u16=65535,
            emitted=False,
        )
        dropped_row = WeightEmissionRow(
            miner_hotkey="miner-a",
            uid=4,
            blended_score=Decimal("0.5"),
            weight_norm_precap=Decimal("0.5"),
            weight_processed=Decimal("0"),
            weight_u16=None,
            emitted=False,
        )
        confirmed_batch = _record_confirmation(
            storage,
            status="submitted",
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=240,
            baseline_last_update_block=90,
            rows=(row, dropped_row),
        )
        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(
                block=110,
                last_update_block=110,
                weights=((3, 65535),),
            ),
            finality_margin_blocks=0,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )
        failed_batch = storage.record_weight_emission(
            schema_id=RISK_SCHEMA_ID,
            round_id=None,
            emitted_at_iso="2026-08-09T00:02:00+00:00",
            block=111,
            min_allowed_weights=1,
            max_weight_limit=Decimal("1"),
            metagraph_size=1,
            status="failed",
            rows=(
                WeightEmissionRow(
                    miner_hotkey="failed-miner",
                    uid=5,
                    blended_score=Decimal("1"),
                    weight_norm_precap=Decimal("1"),
                    weight_processed=Decimal("1"),
                    weight_u16=1,
                    emitted=False,
                ),
                WeightEmissionRow(
                    miner_hotkey="failed-dropped-miner",
                    uid=6,
                    blended_score=Decimal("0"),
                    weight_norm_precap=Decimal("0"),
                    weight_processed=Decimal("0"),
                    weight_u16=None,
                    emitted=False,
                ),
            ),
            confirmation_state="failed",
        )

        with pytest.raises(IntegrityError), storage._engine.begin() as connection:
            connection.execute(
                update(weight_emission_rows)
                .where(weight_emission_rows.c.batch_id == confirmed_batch)
                .values(batch_id=failed_batch)
            )

        for uid in (5, 6):
            with pytest.raises(IntegrityError), storage._engine.begin() as connection:
                connection.execute(
                    update(weight_emission_rows)
                    .where(
                        weight_emission_rows.c.batch_id == failed_batch,
                        weight_emission_rows.c.uid == uid,
                    )
                    .values(batch_id=confirmed_batch)
                )

        with pytest.raises(IntegrityError), storage._engine.begin() as connection:
            connection.execute(
                update(weight_emission_batches)
                .where(weight_emission_batches.c.id == confirmed_batch)
                .values(confirmation_state="unconfirmed", confirmed_at=None)
            )

        second_confirmed_batch = _record_confirmation(
            storage,
            status="submitted",
            submission_block=120,
            confirmation_state="submitted",
            confirmation_deadline_block=260,
            baseline_last_update_block=110,
            rows=(),
        )
        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(block=121, last_update_block=121),
            finality_margin_blocks=0,
            confirmed_at_iso="2026-08-09T00:03:00+00:00",
        )

        with storage._engine.connect() as connection:
            confirmed_values = dict(
                connection.execute(
                    select(weight_emission_batches).where(
                        weight_emission_batches.c.id == second_confirmed_batch
                    )
                )
                .one()
                ._mapping
            )
        confirmed_values["confirmed_at"] = "forged"

        with pytest.raises(IntegrityError), storage._engine.begin() as connection:
            connection.execute(
                insert(weight_emission_batches)
                .prefix_with("OR REPLACE")
                .values(**confirmed_values)
            )

        with pytest.raises(IntegrityError), storage._engine.begin() as connection:
            connection.execute(
                update(weight_emission_rows)
                .where(weight_emission_rows.c.batch_id == confirmed_batch)
                .values(batch_id=second_confirmed_batch)
            )

        with pytest.raises(IntegrityError), storage._engine.begin() as connection:
            connection.execute(
                update(weight_emission_rows)
                .where(weight_emission_rows.c.batch_id == confirmed_batch)
                .values(batch_id=second_confirmed_batch, emitted=False)
            )

        with pytest.raises(IntegrityError), storage._engine.begin() as connection:
            connection.execute(
                update(weight_emission_batches)
                .where(weight_emission_batches.c.id == confirmed_batch)
                .values(confirmation_state=None, confirmed_at=None)
            )

        with pytest.raises(IntegrityError), storage._engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE weight_emission_rows SET emitted = 2 "
                    "WHERE batch_id = :batch_id"
                ),
                {"batch_id": confirmed_batch},
            )

        with pytest.raises(IntegrityError), storage._engine.begin() as connection:
            connection.execute(
                delete(weight_emission_rows).where(
                    weight_emission_rows.c.batch_id == confirmed_batch
                )
            )

        with pytest.raises(IntegrityError), storage._engine.begin() as connection:
            connection.execute(
                insert(weight_emission_rows).values(
                    batch_id=confirmed_batch,
                    miner_hotkey="late-miner",
                    uid=5,
                    weight_processed_text="1",
                    weight_u16=1,
                    emitted=False,
                )
            )

        with pytest.raises(IntegrityError), storage._engine.begin() as connection:
            connection.execute(
                update(weight_emission_rows)
                .where(
                    weight_emission_rows.c.batch_id == confirmed_batch,
                    weight_emission_rows.c.uid == 4,
                )
                .values(weight_u16=1)
            )

        with pytest.raises(IntegrityError), storage._engine.begin() as connection:
            connection.execute(
                update(weight_emission_rows)
                .where(
                    weight_emission_rows.c.batch_id == confirmed_batch,
                    weight_emission_rows.c.uid == 3,
                )
                .values(
                    blended_score_text="0",
                    weight_norm_precap_text="0",
                    weight_processed_text="0",
                )
            )

        with pytest.raises(IntegrityError), storage._engine.begin() as connection:
            connection.execute(
                update(weight_emission_batches)
                .where(weight_emission_batches.c.id == confirmed_batch)
                .values(intent_hash="tampered")
            )

        with pytest.raises(IntegrityError), storage._engine.begin() as connection:
            connection.execute(
                delete(weight_emission_batches).where(
                    weight_emission_batches.c.id == second_confirmed_batch
                )
            )

    def test_confirmation_rejects_incoherent_finalized_snapshot(
        self, storage: Storage
    ) -> None:
        _record_confirmation(
            storage,
            status="submitted",
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=240,
            baseline_last_update_block=90,
        )

        with pytest.raises(ValueError, match="last update exceeds finalized block"):
            storage.resolve_weight_emission_confirmations(
                schema_id=RISK_SCHEMA_ID,
                snapshot=_chain_snapshot(block=110, last_update_block=111),
                finality_margin_blocks=12,
                confirmed_at_iso="2026-08-09T00:01:00+00:00",
            )

    def test_open_confirmation_rejects_incoherent_deadline_bounds(
        self, storage: Storage
    ) -> None:
        with pytest.raises(ValueError, match="submission block precedes baseline"):
            _record_confirmation(
                storage,
                status="submitted",
                submission_block=89,
                confirmation_state="submitted",
                confirmation_deadline_block=229,
                baseline_last_update_block=90,
            )

        with pytest.raises(ValueError, match="confirmation deadline is unbounded"):
            _record_confirmation(
                storage,
                status="submitted",
                submission_block=100,
                confirmation_state="submitted",
                confirmation_deadline_block=357,
                baseline_last_update_block=90,
            )

    def test_last_update_at_submission_boundary_does_not_confirm(
        self, storage: Storage
    ) -> None:
        _record_confirmation(
            storage,
            submission_block=122,
            confirmation_state="submitted",
            confirmation_deadline_block=250,
            baseline_last_update_block=100,
            status="submitted",
        )

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(block=123, last_update_block=122),
            finality_margin_blocks=0,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "submitted"

    def test_post_mortality_update_cannot_confirm_expired_attempt(
        self, storage: Storage
    ) -> None:
        _record_confirmation(
            storage,
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=240,
            baseline_last_update_block=90,
            status="submitted",
        )

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(block=241, last_update_block=229),
            finality_margin_blocks=12,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "unconfirmed"

    def test_submitted_batch_becomes_unconfirmed_after_deadline(
        self, storage: Storage
    ) -> None:
        _record_confirmation(
            storage,
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=228,
            baseline_last_update_block=90,
            status="submitted",
        )

        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(block=229, last_update_block=99),
            finality_margin_blocks=0,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "unconfirmed"
        assert batch["confirmed_at"] is None

    def test_submission_without_a_sampled_block_never_claims_confirmation(
        self, storage: Storage
    ) -> None:
        validator = _audit_validator(storage)

        validator._on_weights_emitted(
            _attempt(
                block=None,
                submission_block=None,
                baseline_last_update_block=None,
                period_blocks=None,
                confirmation_state=None,
            )
        )

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["status"] == "submitted"
        assert batch["confirmation_state"] is None
        assert batch["submission_block"] is None

    def test_weight_history_marks_submitted_rows_as_not_confirmed(
        self, storage: Storage
    ) -> None:
        validator = _audit_validator(storage)
        validator._on_weights_emitted(_attempt())

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)

        assert batch["confirmation_state"] == "submitted"
        rows = batch["rows"]
        assert isinstance(rows, list)
        for row in rows:
            assert isinstance(row, dict)
            assert row["confirmed"] is False

    def test_confirmation_health_reports_open_and_unconfirmed_batches(
        self, storage: Storage
    ) -> None:
        for submission_block, deadline, state in (
            (100, 228, "unconfirmed"),
            (200, 328, "submitted"),
        ):
            _record_confirmation(
                storage,
                status="submitted",
                submission_block=submission_block,
                confirmation_state=state,
                confirmation_deadline_block=deadline,
                baseline_last_update_block=submission_block - 1,
            )
        storage.resolve_weight_emission_confirmations(
            schema_id=RISK_SCHEMA_ID,
            snapshot=_chain_snapshot(block=229, last_update_block=99),
            finality_margin_blocks=0,
            confirmed_at_iso="2026-08-09T00:01:00+00:00",
        )

        health = storage.weight_emission_confirmation_health(
            schema_id=RISK_SCHEMA_ID, current_block=229
        )

        assert health.open_submissions == 1
        assert health.oldest_open_age_blocks == 29
        assert health.latest_unconfirmed_submission_block == 100

    def test_confirmation_health_counts_durable_failures(
        self, storage: Storage
    ) -> None:
        storage.record_weight_emission(
            schema_id=RISK_SCHEMA_ID,
            round_id=None,
            emitted_at_iso="2026-08-09T00:00:00+00:00",
            block=100,
            min_allowed_weights=1,
            max_weight_limit=Decimal("1"),
            metagraph_size=1,
            status="failed",
            rows=(),
            confirmation_state="failed",
        )

        health = storage.weight_emission_confirmation_health(
            schema_id=RISK_SCHEMA_ID, current_block=100
        )

        assert health.failed_submissions_total == 1

    def test_successful_resync_confirms_submitted_batch_after_restart(
        self, storage: Storage
    ) -> None:
        first_validator = _audit_validator(storage)
        first_validator._on_weights_emitted(_attempt())

        restarted_validator = _audit_validator(storage)
        restarted_validator.uid = 0
        restarted_validator.metagraph = MagicMock()
        restarted_validator.metagraph.last_update = [124]
        restarted_validator.metagraph.hotkeys = ["validator-hotkey"]
        restarted_validator.metagraph.__dict__["block"] = 136
        restarted_validator.wallet = MagicMock()
        restarted_validator.wallet.hotkey.ss58_address = "validator-hotkey"
        restarted_validator._last_set_weights_ok = None
        restarted_validator._consecutive_set_weights_failures = 2
        _configure_finalized_chain(
            restarted_validator,
            finalized_block=136,
            last_updates=(124,),
            weights=((0, ((0, 65535), (2, 43689))),),
        )

        restarted_validator._on_metagraph_synced()

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "confirmed"
        assert batch["confirmed_at"] is not None
        assert restarted_validator._last_set_weights_ok is not None
        assert restarted_validator._consecutive_set_weights_failures == 0

    def test_resync_confirms_reveal_from_second_predeadline_scan_batch(
        self, storage: Storage
    ) -> None:
        _record_confirmation(
            storage,
            status="submitted",
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=240,
            cr4_reveal_deadline_block=300,
            baseline_last_update_block=90,
            rows=(
                WeightEmissionRow(
                    miner_hotkey="miner-a",
                    uid=3,
                    blended_score=Decimal("1"),
                    weight_norm_precap=Decimal("1"),
                    weight_processed=Decimal("1"),
                    weight_u16=65535,
                    emitted=False,
                ),
            ),
            submission_mode="cr4",
            commitment_hash="abcd",
            reveal_round=42,
        )
        validator = _audit_validator(storage)
        validator.uid = 0
        validator.wallet = MagicMock()
        validator.wallet.hotkey.ss58_address = VALIDATOR_HOTKEY
        _configure_finalized_chain(
            validator,
            finalized_block=160,
            last_updates=(108,),
            weights=((0, ((3, 65535),)),),
        )
        validator.gated_subtensor.timelocked_weight_reveals_between.side_effect = (
            lambda **window: (
                (150,) if window["start_block"] <= 150 <= window["end_block"] else ()
            )
        )

        validator._on_metagraph_synced()

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "confirmed"

    def test_resync_bounds_predeadline_scan_catchup(self, storage: Storage) -> None:
        _record_confirmation(
            storage,
            status="submitted",
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=240,
            cr4_reveal_deadline_block=500,
            baseline_last_update_block=90,
            submission_mode="cr4",
            commitment_hash="abcd",
            reveal_round=42,
        )
        validator = _audit_validator(storage)
        validator.uid = 0
        validator.wallet = MagicMock()
        validator.wallet.hotkey.ss58_address = VALIDATOR_HOTKEY
        _configure_finalized_chain(
            validator,
            finalized_block=400,
            last_updates=(108,),
        )

        validator._on_metagraph_synced()

        batch, _rows = _recorded_emission(storage)
        assert batch["confirmation_state"] == "submitted"
        assert batch["reveal_scan_cursor_block"] == 195

    def test_failed_cr4_reveal_batch_does_not_advance_cursor(
        self, storage: Storage
    ) -> None:
        batch_id = _record_confirmation(
            storage,
            status="submitted",
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=240,
            cr4_reveal_deadline_block=300,
            baseline_last_update_block=90,
            submission_mode="cr4",
            commitment_hash="abcd",
            reveal_round=42,
        )
        validator = _audit_validator(storage)
        validator.uid = 0
        validator.wallet = MagicMock()
        validator.wallet.hotkey.ss58_address = VALIDATOR_HOTKEY
        _configure_finalized_chain(
            validator,
            finalized_block=350,
            last_updates=(108,),
        )
        validator.gated_subtensor.timelocked_weight_reveals_between.side_effect = (
            RuntimeError("provider unavailable")
        )

        with pytest.raises(RuntimeError, match="provider unavailable"):
            validator._on_metagraph_synced()

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "submitted"
        assert storage.cr4_reveal_scan_window(
            schema_id=RISK_SCHEMA_ID, finalized_block=350
        ) == Cr4RevealScanWindow(batch_id, 100, 131, False)

    @pytest.mark.parametrize("uid", [-1, 2])
    def test_invalid_uid_cannot_resolve_open_submission(
        self, storage: Storage, uid: int
    ) -> None:
        validator = _audit_validator(storage)
        validator._on_weights_emitted(_attempt())
        validator.uid = uid
        validator.metagraph = MagicMock()
        validator.metagraph.last_update = [0, 999]
        validator.metagraph.hotkeys = ["other-a", "other-b"]
        validator.metagraph.__dict__["block"] = 1_000
        validator.wallet = MagicMock()
        validator.wallet.hotkey.ss58_address = "validator-hotkey"
        _configure_finalized_chain(
            validator,
            finalized_block=1_000,
            last_updates=(0, 999),
        )

        validator._on_metagraph_synced()

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "submitted"

    def test_latest_hotkey_mismatch_does_not_override_finalized_identity(
        self, storage: Storage
    ) -> None:
        validator = _audit_validator(storage)
        validator._on_weights_emitted(_attempt())
        validator.uid = 0
        validator.metagraph = MagicMock()
        validator.metagraph.last_update = [999]
        validator.metagraph.hotkeys = ["other-hotkey"]
        validator.metagraph.__dict__["block"] = 1_000
        validator.wallet = MagicMock()
        validator.wallet.hotkey.ss58_address = "validator-hotkey"
        _configure_finalized_chain(
            validator,
            finalized_block=136,
            last_updates=(124,),
            weights=((0, ((0, 65535), (2, 43689))),),
        )

        validator._on_metagraph_synced()

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "confirmed"

    def test_finalized_uid_hotkey_mismatch_cannot_resolve_open_submission(
        self, storage: Storage
    ) -> None:
        validator = _audit_validator(storage)
        validator._on_weights_emitted(_attempt())
        validator.uid = 0
        validator.metagraph = MagicMock()
        validator.metagraph.hotkeys = [VALIDATOR_HOTKEY]
        validator.wallet = MagicMock()
        validator.wallet.hotkey.ss58_address = VALIDATOR_HOTKEY
        _configure_finalized_chain(
            validator,
            finalized_block=136,
            last_updates=(124,),
            weights=((0, ((0, 65535), (2, 43689))),),
            hotkeys=((0, "replacement-validator"), (2, "hk-pad")),
        )

        validator._on_metagraph_synced()

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "submitted"

    def test_finalized_target_hotkey_mismatch_cannot_resolve_open_submission(
        self, storage: Storage
    ) -> None:
        row = WeightEmissionRow(
            miner_hotkey="miner-a",
            uid=3,
            blended_score=Decimal("1"),
            weight_norm_precap=Decimal("1"),
            weight_processed=Decimal("1"),
            weight_u16=65535,
            emitted=False,
        )
        _record_confirmation(
            storage,
            status="submitted",
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=240,
            baseline_last_update_block=90,
            rows=(row,),
        )
        validator = _audit_validator(storage)
        validator.uid = 0
        validator.metagraph = MagicMock()
        validator.metagraph.hotkeys = [VALIDATOR_HOTKEY]
        validator.wallet = MagicMock()
        validator.wallet.hotkey.ss58_address = VALIDATOR_HOTKEY
        _configure_finalized_chain(
            validator,
            finalized_block=150,
            last_updates=(110,),
            weights=((0, ((3, 65535),)),),
            hotkeys=((0, VALIDATOR_HOTKEY), (3, "replacement-miner")),
        )

        validator._on_metagraph_synced()

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "submitted"

    def test_tensor_backed_metagraph_block_degrades_overdue_open_attempt(
        self, storage: Storage
    ) -> None:
        _record_confirmation(
            storage,
            status="submitted",
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=240,
            baseline_last_update_block=90,
        )
        validator = _audit_validator(storage)
        validator.metagraph = MagicMock()
        validator.metagraph.__dict__["block"] = np.array([241])
        validator.rpc_gate = MagicMock()
        validator.rpc_gate.snapshot.return_value = MagicMock(
            degraded=False, abandoned_generations=0
        )
        validator._service = MagicMock(
            consecutive_universe_failures=0,
            last_universe_error=None,
            consecutive_resolution_failures=0,
            last_resolution_error=None,
            consecutive_empty_scored_rounds=0,
            last_empty_scored_round=None,
        )
        validator._consecutive_set_weights_failures = 0
        validator._last_set_weights_ok = None
        validator._tick_failures = 0
        validator._last_tick_ok = None
        validator._last_tick_error = None
        validator._last_tick_monotonic = None
        validator._started_monotonic = 0.0
        validator.config.endure.health_startup_grace_seconds = 1
        validator.thread = MagicMock()
        validator.thread.is_alive.return_value = True

        health = validator.runtime_health()

        assert health.get("oldest_open_weight_submission_age_blocks") == 141
        assert health.get("weight_emission_degraded") is True

    def test_runtime_health_uses_persisted_confirmation_deadline(
        self, storage: Storage
    ) -> None:
        _record_confirmation(
            storage,
            status="submitted",
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=260,
            baseline_last_update_block=90,
        )
        validator = _audit_validator(storage)
        validator.metagraph = MagicMock()
        validator.metagraph.__dict__["block"] = np.array([241])
        validator.rpc_gate = MagicMock()
        validator.rpc_gate.snapshot.return_value = MagicMock(
            degraded=False, abandoned_generations=0
        )
        validator._service = MagicMock(
            consecutive_universe_failures=0,
            last_universe_error=None,
            consecutive_resolution_failures=0,
            last_resolution_error=None,
            consecutive_empty_scored_rounds=0,
            last_empty_scored_round=None,
        )
        validator._consecutive_set_weights_failures = 0
        validator._last_set_weights_ok = None
        validator._tick_failures = 0
        validator._last_tick_ok = None
        validator._last_tick_error = None
        validator._last_tick_monotonic = None
        validator._started_monotonic = 0.0
        validator.config.endure.health_startup_grace_seconds = 1
        validator.thread = MagicMock()
        validator.thread.is_alive.return_value = True

        assert validator.runtime_health().get("weight_emission_degraded") is False

        validator.metagraph.__dict__["block"] = np.array([261])
        assert validator.runtime_health().get("weight_emission_degraded") is True

    def test_runtime_health_exposes_late_rpc_completion_totals(
        self, storage: Storage
    ) -> None:
        validator = _audit_validator(storage)
        validator.metagraph = MagicMock()
        validator.metagraph.__dict__["block"] = 100
        validator.rpc_gate = MagicMock()
        validator.rpc_gate.snapshot.return_value = MagicMock(
            adaptive_rate=1.0,
            degraded=False,
            rate_limited_total=0,
            deferred_total=0,
            abandoned_generations=0,
            late_completions_total=4,
            late_set_weights_completions_total=1,
        )
        validator._service = MagicMock(
            consecutive_universe_failures=0,
            last_universe_error=None,
            consecutive_resolution_failures=0,
            last_resolution_error=None,
            consecutive_empty_scored_rounds=0,
            last_empty_scored_round=None,
        )
        validator._consecutive_set_weights_failures = 0
        validator._last_set_weights_ok = None
        validator._tick_failures = 0
        validator._last_tick_ok = None
        validator._last_tick_error = None
        validator._last_tick_monotonic = None
        validator._started_monotonic = 0.0
        validator.config.endure.health_startup_grace_seconds = 1
        validator.thread = MagicMock()
        validator.thread.is_alive.return_value = True

        rpc_health = validator.runtime_health()["rpc_gate"]

        assert rpc_health["late_completions_total"] == 4
        assert rpc_health["late_set_weights_completions_total"] == 1

    def test_unknown_cached_block_degrades_open_attempt_after_startup_grace(
        self, storage: Storage
    ) -> None:
        _record_confirmation(
            storage,
            status="submitted",
            submission_block=100,
            confirmation_state="submitted",
            confirmation_deadline_block=240,
            baseline_last_update_block=90,
        )
        validator = _audit_validator(storage)
        validator.metagraph = MagicMock()
        validator.metagraph.__dict__["block"] = np.array([240, 241])
        validator.rpc_gate = MagicMock()
        validator.rpc_gate.snapshot.return_value = MagicMock(
            degraded=False, abandoned_generations=0
        )
        validator._service = MagicMock(
            consecutive_universe_failures=0,
            last_universe_error=None,
            consecutive_resolution_failures=0,
            last_resolution_error=None,
            consecutive_empty_scored_rounds=0,
            last_empty_scored_round=None,
        )
        validator._consecutive_set_weights_failures = 0
        validator._last_set_weights_ok = None
        validator._tick_failures = 0
        validator._last_tick_ok = None
        validator._last_tick_error = None
        validator._last_tick_monotonic = None
        validator._started_monotonic = 0.0
        validator.config.endure.health_startup_grace_seconds = 1
        validator.thread = MagicMock()
        validator.thread.is_alive.return_value = True

        assert validator.runtime_health().get("weight_emission_degraded") is True

    def test_different_runtime_identity_cannot_confirm_stored_submission(
        self, storage: Storage
    ) -> None:
        first_validator = _audit_validator(storage)
        first_validator.uid = 0
        first_validator.wallet = MagicMock()
        first_validator.wallet.hotkey.ss58_address = "validator-a"
        first_validator._on_weights_prepared(
            _attempt(
                confirmation_state="prepared",
                validator_hotkey="validator-a",
            )
        )

        restarted_validator = _audit_validator(storage)
        restarted_validator.uid = 1
        restarted_validator.wallet = MagicMock()
        restarted_validator.wallet.hotkey.ss58_address = "validator-b"
        restarted_validator.metagraph = MagicMock()
        restarted_validator.metagraph.last_update = [0, 124]
        restarted_validator.metagraph.hotkeys = ["validator-a", "validator-b"]
        restarted_validator.metagraph.__dict__["block"] = 136
        _configure_finalized_chain(
            restarted_validator,
            finalized_block=136,
            last_updates=(0, 124),
            weights=((1, ((0, 65535), (2, 43689))),),
        )

        restarted_validator._on_metagraph_synced()

        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "prepared"

    def test_validator_does_not_submit_while_a_batch_is_unconfirmed(
        self, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        validator = _audit_validator(storage)
        validator.scores = [Decimal("0.5")]
        _record_confirmation(
            storage,
            submission_block=122,
            confirmation_state="submitted",
            confirmation_deadline_block=250,
            baseline_last_update_block=100,
            status="submitted",
        )
        emit = MagicMock()
        monkeypatch.setattr(BaseValidatorNeuron, "set_weights", emit)

        validator.set_weights()

        emit.assert_not_called()

    def test_startup_fence_outlives_prior_sdk_mortality(
        self, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        validator = _audit_validator(storage)
        validator.scores = [Decimal("0.5")]
        validator._weight_emission_startup_fence_block = None
        validator.gated_subtensor = MagicMock()
        validator.gated_subtensor.commit_reveal_enabled.return_value = True
        validator.gated_subtensor.cr4_reveal_deadline_at.return_value = 732
        current_block = MagicMock(return_value=100)
        validator._safe_block = current_block
        emit = MagicMock()
        monkeypatch.setattr(BaseValidatorNeuron, "set_weights", emit)

        validator.set_weights()
        current_block.return_value = 732
        validator.set_weights()
        current_block.return_value += 1
        validator.set_weights()

        emit.assert_called_once_with()

        restarted = _audit_validator(storage)
        restarted.scores = [Decimal("0.5")]
        restarted._weight_emission_startup_fence_block = None
        restarted.gated_subtensor = MagicMock()
        restarted.gated_subtensor.commit_reveal_enabled.return_value = True
        restarted.gated_subtensor.cr4_reveal_deadline_at.return_value = 900
        restarted._safe_block = MagicMock(return_value=733)

        restarted.set_weights()

        assert emit.call_count == 2
        restarted.gated_subtensor.cr4_reveal_deadline_at.assert_not_called()


class _RecordingNeuron(BaseValidatorNeuron):
    def __init__(self) -> None:
        self.prepared_attempts: list[WeightEmissionAttempt] = []
        self.attempts: list[WeightEmissionAttempt] = []
        self.completed_batch_ids: list[int | None] = []

    def _on_weights_prepared(self, attempt: WeightEmissionAttempt) -> int:
        self.prepared_attempts.append(attempt)
        return 41

    def _on_weights_emitted(
        self, attempt: WeightEmissionAttempt, batch_id: int | None
    ) -> None:
        self.attempts.append(attempt)
        self.completed_batch_ids.append(batch_id)

    async def forward(self) -> None:
        raise NotImplementedError("test double — never driven by the run loop")


class _ExplodingNeuron(_RecordingNeuron):
    def _on_weights_emitted(
        self, attempt: WeightEmissionAttempt, batch_id: int | None
    ) -> None:
        raise RuntimeError("audit db locked")


class _PrepareExplodingNeuron(_RecordingNeuron):
    def _on_weights_prepared(self, attempt: WeightEmissionAttempt) -> int:
        raise RuntimeError("audit db locked")


def _base_neuron(
    *,
    set_weights_result: ExtrinsicResponse | Exception | tuple[bool, str],
    neuron_cls: type[_RecordingNeuron] = _RecordingNeuron,
) -> tuple[_RecordingNeuron, MagicMock]:
    neuron = neuron_cls.__new__(neuron_cls)
    neuron.prepared_attempts = []
    neuron.attempts = []
    neuron.completed_batch_ids = []
    neuron._consecutive_provider_throttles = 0
    neuron.scores = [Decimal("0.75"), Decimal("0.25")]
    metagraph = MagicMock()
    metagraph.uids = np.array([0, 1])
    metagraph.hotkeys = ["hk-a", "hk-b"]
    metagraph.n = 2
    metagraph.last_update = [5, 0]
    neuron.metagraph = metagraph
    neuron.uid = 0
    subtensor = MagicMock()
    subtensor.min_allowed_weights.return_value = 1
    subtensor.max_weight_limit.return_value = "1.0"
    subtensor.get_current_block.return_value = 7
    subtensor.get_block_hash.return_value = CHAIN_IDENTITY
    subtensor.commit_reveal_enabled.return_value = False
    if isinstance(set_weights_result, Exception):
        subtensor.set_weights.side_effect = set_weights_result
    else:
        subtensor.set_weights.return_value = (
            set_weights_result
            if isinstance(set_weights_result, ExtrinsicResponse)
            else ExtrinsicResponse(*set_weights_result)
        )
    neuron.subtensor = subtensor
    neuron.gated_subtensor = subtensor
    config = MagicMock()
    config.netuid = 1
    neuron.config = config
    neuron.wallet = MagicMock()
    neuron.wallet.hotkey.ss58_address = "hk-a"
    return neuron, subtensor


class TestSetWeightsAttemptWrapping:
    def test_postcall_deadline_extension_is_bounded(self) -> None:
        neuron, subtensor = _base_neuron(set_weights_result=(True, "ok"))
        fallback = (
            7 + WEIGHT_EMISSION_PERIOD_BLOCKS + WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS
        )
        subtensor.get_current_block.return_value = 10_000

        deadline, reveal_deadline = neuron._post_submission_deadlines(
            _attempt(confirmation_deadline_block=fallback)
        )

        assert deadline == 7 + (2 * WEIGHT_EMISSION_PERIOD_BLOCKS)
        assert reveal_deadline is None

    def test_provider_throttle_persists_ambiguous_single_flight(
        self, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        validator = _audit_validator(storage)
        validator.scores = [Decimal("0.75"), Decimal("0.25")]
        validator.uid = 0
        validator.wallet = MagicMock()
        validator.wallet.hotkey.ss58_address = "hk-a"
        validator.metagraph = MagicMock()
        validator.metagraph.uids = np.array([0, 1])
        validator.metagraph.hotkeys = ["hk-a", "hk-b"]
        validator.metagraph.last_update = [5, 0]
        validator.metagraph.n = 2
        validator.subtensor = MagicMock()
        validator.subtensor.min_allowed_weights.return_value = 1
        validator.subtensor.max_weight_limit.return_value = "1.0"
        validator.subtensor.get_current_block.return_value = 7
        validator.subtensor.get_block_hash.return_value = CHAIN_IDENTITY
        validator.subtensor.commit_reveal_enabled.return_value = False
        validator.subtensor.set_weights.side_effect = RateLimited(
            retry_after_monotonic=2_000.0,
            provider_limited=True,
        )
        validator.gated_subtensor = validator.subtensor
        validator._safe_block = MagicMock(return_value=7)
        validator._consecutive_provider_throttles = 0

        # When the provider throttles after the extrinsic may have been sent.
        BaseValidatorNeuron.set_weights(validator)

        # Then preserve both the reconnect signal and the ambiguous single-flight.
        assert validator._consecutive_provider_throttles == 1
        [batch] = storage.weight_emission_history(RISK_SCHEMA_ID)
        assert batch["confirmation_state"] == "ambiguous"
        assert storage.has_open_weight_emission_confirmation(schema_id=RISK_SCHEMA_ID)
        emit = MagicMock()
        monkeypatch.setattr(BaseValidatorNeuron, "set_weights", emit)
        validator.set_weights()
        emit.assert_not_called()
        assert validator.subtensor.set_weights.call_count == 1

    def test_set_weights_timeout_stays_ambiguous_and_replaces_transport(self) -> None:
        neuron, subtensor = _base_neuron(
            set_weights_result=ChainRpcStalled(
                operation_name="set_weights", timeout_seconds=90
            )
        )
        reconnect = MagicMock()
        neuron._reconnect_subtensor = reconnect

        neuron.set_weights()

        [attempt] = neuron.attempts
        assert attempt.status == "error"
        assert attempt.confirmation_state == "ambiguous"
        assert subtensor.set_weights.call_count == 1
        reconnect.assert_called_once_with(reason="set_weights timeout")

    def test_submitted_success_records_submitted_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response = ExtrinsicResponse(True, "ok")
        response.data = {
            "commit_for_reveal": bytes.fromhex("abcd"),
            "reveal_round": 42,
        }
        neuron, subtensor = _base_neuron(set_weights_result=response)
        subtensor.commit_reveal_enabled.return_value = True
        subtensor.cr4_reveal_deadline_at.side_effect = [732, 1092]
        info_log = MagicMock()
        monkeypatch.setattr(bt.logging, "info", info_log)

        neuron.set_weights()

        [attempt] = neuron.attempts
        [prepared] = neuron.prepared_attempts
        assert prepared.confirmation_state == "prepared"
        assert prepared.baseline_last_update_block == 5
        assert prepared.period_blocks == WEIGHT_EMISSION_PERIOD_BLOCKS
        assert prepared.confirmation_deadline_block == (
            7 + WEIGHT_EMISSION_PERIOD_BLOCKS + WEIGHT_EMISSION_FINALITY_MARGIN_BLOCKS
        )
        assert prepared.cr4_reveal_deadline_block == 732
        assert attempt.status == "submitted"
        assert neuron.spec_version == 1000
        assert attempt.protocol_version_key == CURRENT_VERSION_KEY
        assert attempt.intent_hash is not None
        assert attempt.intent_hash.startswith(f"{CURRENT_VERSION_KEY}:")
        assert attempt.confirmation_state == "submitted"
        assert attempt.submission_mode == "cr4"
        assert attempt.commitment_hash == "abcd"
        assert attempt.reveal_round == 42
        assert attempt.cr4_reveal_deadline_block == 1092
        assert attempt.submission_block == 7
        assert attempt.hotkeys == ("hk-a", "hk-b")
        assert attempt.min_allowed_weights == 1
        assert attempt.max_weight_limit == Decimal("1.0")
        assert len(attempt.uint_uids) == len(attempt.uint_weights)
        assert attempt.uint_weights
        subtensor.min_allowed_weights.assert_called_once()
        subtensor.max_weight_limit.assert_called_once()
        assert subtensor.get_current_block.call_count == 3
        subtensor.get_block_hash.assert_called_once_with(0)
        subtensor.commit_reveal_enabled.assert_called_once_with(netuid=1)
        subtensor.set_weights.assert_called_once_with(
            wallet=neuron.wallet,
            netuid=1,
            uids=list(attempt.uint_uids),
            weights=list(attempt.uint_weights),
            wait_for_finalization=False,
            wait_for_inclusion=False,
            max_attempts=1,
            mev_protection=False,
            period=WEIGHT_EMISSION_PERIOD_BLOCKS,
            raise_error=True,
            version_key=CURRENT_VERSION_KEY,
        )
        assert neuron.completed_batch_ids == [41]
        info_log.assert_any_call("set_weights submitted: ok")

    def test_failed_status_reaches_hook(self) -> None:
        neuron, _subtensor = _base_neuron(set_weights_result=(False, "boom"))
        neuron.set_weights()
        [attempt] = neuron.attempts
        assert attempt.status == "failed"
        assert attempt.uint_weights

    def test_failed_status_logs_sanitized_sdk_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        neuron, _subtensor = _base_neuron(
            set_weights_result=(False, "denied wss://user:secret@localhost/ws")
        )
        error_log = MagicMock()
        monkeypatch.setattr(bt.logging, "error", error_log)

        neuron.set_weights()

        error_log.assert_called_once_with(
            "set_weights failed: denied <redacted-endpoint>"
        )

    def test_set_weights_exception_is_not_retried_as_a_new_submission(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rpc_error = RuntimeError({"code": -32029, "message": "chain wedged"})
        neuron, subtensor = _base_neuron(set_weights_result=rpc_error)
        error_log = MagicMock()
        monkeypatch.setattr(bt.logging, "error", error_log)

        neuron.set_weights()

        [attempt] = neuron.attempts
        assert attempt.status == "error"
        assert attempt.submission_block == 7
        assert attempt.processed_uids
        assert attempt.min_allowed_weights == 1
        assert subtensor.set_weights.call_count == 1
        [error_call] = error_log.call_args_list
        assert "outcome is ambiguous; not retrying" in error_call.args[0]
        assert type(rpc_error).__name__ in error_call.args[0]
        assert repr(rpc_error.args) not in error_call.args[0]

    def test_set_weights_exception_records_ambiguous_error_once(self) -> None:
        rpc_error = RuntimeError({"code": -32029, "message": "chain wedged"})
        neuron, subtensor = _base_neuron(set_weights_result=rpc_error)
        neuron.set_weights()

        [attempt] = neuron.attempts
        assert attempt.status == "error"
        assert subtensor.set_weights.call_count == 1

    def test_rate_limited_set_weights_remains_deferred_to_sync(self) -> None:
        neuron, subtensor = _base_neuron(
            set_weights_result=RateLimited(
                retry_after_monotonic=2000.0,
                provider_limited=False,
            )
        )

        with pytest.raises(RateLimited):
            neuron.set_weights()

        [attempt] = neuron.attempts
        assert attempt.status == "failed"
        assert attempt.confirmation_state == "failed"
        assert attempt.submission_block == 7
        assert subtensor.set_weights.call_count == 1

    def test_provider_rate_limit_after_invocation_is_ambiguous(self) -> None:
        neuron, subtensor = _base_neuron(
            set_weights_result=RateLimited(
                retry_after_monotonic=2000.0,
                provider_limited=True,
            )
        )

        neuron.set_weights()

        [attempt] = neuron.attempts
        assert attempt.status == "error"
        assert attempt.confirmation_state == "ambiguous"
        assert attempt.submission_block == 7
        assert subtensor.set_weights.call_count == 1

    def test_prepare_failure_prevents_sdk_submission(self) -> None:
        neuron, subtensor = _base_neuron(
            set_weights_result=(True, "ok"), neuron_cls=_PrepareExplodingNeuron
        )

        with pytest.raises(RuntimeError, match="audit db locked"):
            neuron.set_weights()

        subtensor.set_weights.assert_not_called()

    def test_hyperparam_fetch_failure_is_recorded_as_error(self) -> None:
        neuron, subtensor = _base_neuron(set_weights_result=(True, "ok"))
        subtensor.min_allowed_weights.side_effect = RuntimeError("rpc unavailable")
        with pytest.raises(RuntimeError, match="rpc unavailable"):
            neuron.set_weights()
        [attempt] = neuron.attempts
        assert attempt.status == "error"
        assert attempt.min_allowed_weights is None
        assert attempt.processed_uids == ()
        assert neuron._consecutive_set_weights_failures == 1

    def test_abstention_never_fires_hook(self) -> None:
        neuron, _subtensor = _base_neuron(set_weights_result=(True, "ok"))
        neuron.scores = [ZERO, ZERO]
        neuron.set_weights()
        assert neuron.attempts == []

    def test_hook_failure_never_breaks_emission(self) -> None:
        neuron, _subtensor = _base_neuron(
            set_weights_result=(True, "ok"), neuron_cls=_ExplodingNeuron
        )
        neuron.set_weights()
