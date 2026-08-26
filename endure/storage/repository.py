"""Typed persistence over the round and assessment tables (spec §5, §11).

Thin synchronous repository: every mutation takes ``now_iso`` from the caller
(no clock reads), every transition writes through so a validator restart
resumes from the database plus the calendar. Decimal values are stored as
text; this module never constructs floats.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    and_,
    create_engine,
    delete,
    event,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine, RowMapping, make_url

from endure.assessment.coordinates import (
    AssessmentConsensusRow,
    AssessmentCoordinate,
    AssessmentEmaState,
    AssessmentOutputScore,
    AssessmentRealizedTarget,
    AssessmentScoreHistoryRow,
    HorizonKind,
    TargetKind,
)
from endure.assessment.registry import UniverseSnapshot
from endure.protocol.round_engine import RoundWindows
from endure.protocol.version_contract import CURRENT_VERSION_KEY
from endure.protocol.weight_intent import (
    WeightIntentPayload,
    canonical_weight_intent_hash,
)
from endure.storage.tables import (
    assessment_consensus,
    assessment_horizon_resolutions,
    assessment_miner_score_state,
    assessment_output_scores,
    assessment_realized_targets,
    assessment_score_history,
    consensus_bundle_snapshots,
    rounds,
    submissions,
    universe_snapshots,
    weight_emission_batches,
    weight_emission_rows,
    weight_emission_startup_fences,
)

ROUND_STATE_OPEN = "open"
ROUND_STATE_REVEALED = "revealed"
ROUND_STATE_PARTIALLY_SCORED = "partially_scored"
ROUND_STATE_CLOSED = "closed"

POST_EMBARGO_ROUND_STATES = frozenset(
    {ROUND_STATE_REVEALED, ROUND_STATE_PARTIALLY_SCORED, ROUND_STATE_CLOSED}
)
VALID_ROUND_STATES = frozenset({ROUND_STATE_OPEN, *POST_EMBARGO_ROUND_STATES})


@dataclass(frozen=True, slots=True)
class AcceptedAssessmentBundle:
    """Accepted reveal payload for a schema-neutral assessment round."""

    miner_hotkey: str
    bundle_json: str


@dataclass(frozen=True, slots=True)
class AssessmentRoundResolutionProgress:
    """Persisted resolution markers for one unfinished assessment round."""

    round_id: str
    reveal_close_at: datetime
    resolved_horizons: frozenset[int]


WEIGHT_EMISSION_STATUSES = frozenset({"submitted", "failed", "error"})
WEIGHT_EMISSION_CONFIRMATION_STATES = frozenset(
    {"prepared", "submitted", "ambiguous", "confirmed", "unconfirmed", "failed"}
)
OPEN_WEIGHT_EMISSION_CONFIRMATION_STATES = frozenset(
    {"prepared", "submitted", "ambiguous"}
)
CR4_REVEAL_SCAN_BATCH_BLOCKS = 32


@dataclass(frozen=True, slots=True)
class WeightEmissionConfirmationResolution:
    """Counts of durable emission states transitioned by one metagraph snapshot."""

    confirmed: int
    unconfirmed: int


@dataclass(frozen=True, slots=True)
class WeightEmissionConfirmationHealth:
    """Local, provider-neutral summary of durable emission confirmation."""

    last_confirmed_at: str | None
    open_submissions: int
    oldest_open_age_blocks: int | None
    oldest_open_deadline_block: int | None
    latest_unconfirmed_submission_block: int | None
    latest_confirmed_submission_block: int | None
    failed_submissions_total: int


@dataclass(frozen=True, slots=True)
class WeightCommitEvidence:
    validator_hotkey: str
    commit_block: int
    commitment_hash: str
    reveal_round: int


@dataclass(frozen=True, slots=True)
class WeightRevealEvidence:
    validator_hotkey: str
    netuid: int
    reveal_block: int


@dataclass(frozen=True, slots=True)
class Cr4RevealScanWindow:
    batch_id: int
    start_block: int
    end_block: int
    complete: bool


@dataclass(frozen=True, slots=True)
class WeightEmissionChainSnapshot:
    chain_identity: str
    netuid: int
    validator_uid: int
    validator_hotkey: str
    block: int
    last_update_block: int
    weights: tuple[tuple[int, int], ...]
    commitments: tuple[WeightCommitEvidence, ...]
    reveals: tuple[WeightRevealEvidence, ...]
    hotkeys: tuple[tuple[int, str], ...]
    reveal_scan_complete: bool = True


@dataclass(frozen=True, slots=True)
class WeightResolutionDecision:
    confirm: bool = False
    unconfirm: bool = False
    observed_commit_block: int | None = None
    observed_last_update_block: int | None = None
    observed_commitment_hash: str | None = None
    observed_reveal_round: int | None = None


@dataclass(frozen=True, slots=True)
class WeightEmissionRow:
    """One hotkey's audit row for one weight-emission attempt (spec §2)."""

    miner_hotkey: str
    uid: int
    blended_score: Decimal | None
    weight_norm_precap: Decimal | None
    weight_processed: Decimal
    weight_u16: int | None
    emitted: bool
    confirmed: bool = False


def _weight_intent_hash(
    *,
    chain_identity: str,
    netuid: int,
    validator_uid: int,
    validator_hotkey: str,
    targets: Sequence[tuple[int, str, int]],
    protocol_version_key: int | None = None,
) -> str:
    if protocol_version_key is not None:
        return canonical_weight_intent_hash(
            WeightIntentPayload(
                protocol_version_key=protocol_version_key,
                chain_identity=chain_identity,
                netuid=netuid,
                validator_uid=validator_uid,
                validator_hotkey=validator_hotkey,
                targets=tuple(targets),
            )
        )
    payload = json.dumps(
        {
            "chain_identity": chain_identity,
            "netuid": netuid,
            "validator_uid": validator_uid,
            "validator_hotkey": validator_hotkey,
            "targets": sorted(targets),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def _recorded_protocol_version(intent_hash: str) -> int | None:
    if re.fullmatch(r"[0-9a-f]{64}", intent_hash):
        return None
    versioned = re.fullmatch(r"(0|[1-9][0-9]*):[0-9a-f]{64}", intent_hash)
    if versioned is None:
        raise ValueError("weight intent hash has invalid versioned format")
    return int(versioned.group(1))


def _validate_weight_emission_numbers(
    submission_block: int | None,
    baseline_last_update_block: int | None,
    period_blocks: int | None,
    confirmation_deadline_block: int | None,
    cr4_reveal_deadline_block: int | None,
    netuid: int | None,
    validator_uid: int | None,
    reveal_round: int | None,
    commitment_observed_block: int | None,
) -> None:
    non_negative = {
        "submission_block": submission_block,
        "baseline_last_update_block": baseline_last_update_block,
        "confirmation_deadline_block": confirmation_deadline_block,
        "cr4_reveal_deadline_block": cr4_reveal_deadline_block,
        "netuid": netuid,
        "validator_uid": validator_uid,
        "reveal_round": reveal_round,
        "commitment_observed_block": commitment_observed_block,
    }
    invalid = next(
        (
            name
            for name, value in non_negative.items()
            if value is not None and value < 0
        ),
        None,
    )
    if invalid is not None:
        raise ValueError(f"{invalid} must be non-negative")
    if period_blocks is not None and period_blocks <= 0:
        raise ValueError("period_blocks must be positive")
    if (
        submission_block is not None
        and baseline_last_update_block is not None
        and submission_block < baseline_last_update_block
    ):
        raise ValueError("submission block precedes baseline last update")
    if (
        confirmation_deadline_block is not None
        and submission_block is not None
        and period_blocks is not None
        and confirmation_deadline_block < submission_block + period_blocks
    ):
        raise ValueError("confirmation deadline precedes submission mortality")
    if (
        confirmation_deadline_block is not None
        and submission_block is not None
        and period_blocks is not None
        and confirmation_deadline_block > submission_block + 2 * period_blocks
    ):
        raise ValueError("confirmation deadline is unbounded")
    if (
        cr4_reveal_deadline_block is not None
        and confirmation_deadline_block is not None
        and cr4_reveal_deadline_block < confirmation_deadline_block
    ):
        raise ValueError("CR4 reveal deadline precedes inclusion deadline")


def _validate_weight_emission_transport(
    status: str,
    confirmation_state: str | None,
    confirmed_at_iso: str | None,
) -> None:
    if status not in WEIGHT_EMISSION_STATUSES:
        raise ValueError(f"unknown weight emission status {status!r}")
    if (
        confirmation_state is not None
        and confirmation_state not in WEIGHT_EMISSION_CONFIRMATION_STATES
    ):
        raise ValueError(
            f"unknown weight emission confirmation state {confirmation_state!r}"
        )
    if confirmation_state == "confirmed":
        raise ValueError("confirmed emissions may only be created by resolution")
    if (confirmation_state in {"prepared", "ambiguous"} and status != "error") or (
        confirmation_state == "submitted" and status != "submitted"
    ):
        raise ValueError(
            f"confirmation state {confirmation_state!r} does not match "
            f"transport status {status!r}"
        )
    if confirmation_state == "failed" and status not in {"failed", "error"}:
        raise ValueError(
            f"confirmation state 'failed' does not match transport status {status!r}"
        )
    if confirmed_at_iso is not None:
        raise ValueError("confirmed_at may only be set by resolution")


def _validate_weight_emission_evidence(
    confirmation_state: str | None,
    submission_block: int | None,
    baseline_last_update_block: int | None,
    period_blocks: int | None,
    confirmation_deadline_block: int | None,
    chain_identity: str | None,
    netuid: int | None,
    validator_uid: int | None,
    validator_hotkey: str | None,
    submission_mode: str | None,
    intent_hash: str | None,
    protocol_version_key: int | None,
    commitment_hash: str | None,
    reveal_round: int | None,
    rows: Sequence[WeightEmissionRow],
) -> None:
    if submission_mode not in {None, "direct", "cr4"}:
        raise ValueError(f"unknown weight submission mode {submission_mode!r}")
    evidence_fields = (
        chain_identity,
        netuid,
        validator_uid,
        validator_hotkey,
        submission_mode,
        intent_hash,
    )
    if confirmation_state in OPEN_WEIGHT_EMISSION_CONFIRMATION_STATES and (
        submission_block is None
        or baseline_last_update_block is None
        or period_blocks is None
        or confirmation_deadline_block is None
        or any(field is None for field in evidence_fields)
    ):
        raise ValueError(
            "open confirmation requires submission, confirmation deadline, "
            "identity, intent, and period"
        )
    if (commitment_hash is None) != (reveal_round is None):
        raise ValueError("CR4 commitment hash and reveal round must be set together")
    if commitment_hash is not None and submission_mode != "cr4":
        raise ValueError("CR4 commitment metadata requires CR4 submission mode")
    if any(row.emitted or row.confirmed for row in rows):
        raise ValueError("new weight rows cannot be pre-confirmed or emitted")
    if any(field is None for field in evidence_fields):
        return
    if not isinstance(chain_identity, str):
        raise TypeError("chain_identity must be text")
    if not isinstance(netuid, int) or not isinstance(validator_uid, int):
        raise TypeError("netuid and validator_uid must be integers")
    if not isinstance(validator_hotkey, str) or not isinstance(intent_hash, str):
        raise TypeError("validator_hotkey and intent_hash must be text")
    recorded_protocol_version = _recorded_protocol_version(intent_hash)
    if recorded_protocol_version is not None and (
        recorded_protocol_version != CURRENT_VERSION_KEY
        or protocol_version_key != CURRENT_VERSION_KEY
    ):
        raise ValueError(
            "versioned weight intent does not use the current protocol key"
        )
    if recorded_protocol_version is None and (
        confirmation_state in OPEN_WEIGHT_EMISSION_CONFIRMATION_STATES
        or protocol_version_key is not None
    ):
        raise ValueError(
            "legacy weight intent cannot create an open emission or declare a key"
        )
    expected_intent_hash = _weight_intent_hash(
        chain_identity=chain_identity,
        netuid=netuid,
        validator_uid=validator_uid,
        validator_hotkey=validator_hotkey,
        targets=tuple(
            (row.uid, row.miner_hotkey, row.weight_u16)
            for row in rows
            if row.weight_u16 is not None
        ),
        protocol_version_key=recorded_protocol_version,
    )
    if intent_hash != expected_intent_hash:
        raise ValueError("weight intent hash does not match persisted vector")


def _direct_weight_resolution(
    *,
    snapshot: WeightEmissionChainSnapshot,
    submission_block: int,
    baseline_block: int,
    deadline: int | None,
    finality_margin_blocks: int,
    vector_matches: bool,
) -> WeightResolutionDecision:
    expiry_block = (
        deadline - finality_margin_blocks if isinstance(deadline, int) else None
    )
    confirm = (
        isinstance(expiry_block, int)
        and snapshot.last_update_block > max(submission_block, baseline_block)
        and snapshot.last_update_block <= expiry_block
        and vector_matches
    )
    return WeightResolutionDecision(
        confirm=confirm,
        unconfirm=(
            not confirm and isinstance(deadline, int) and snapshot.block > deadline
        ),
    )


def _validate_weight_chain_snapshot(snapshot: WeightEmissionChainSnapshot) -> None:
    if snapshot.block < 0 or snapshot.last_update_block < 0:
        raise ValueError("finalized weight evidence blocks must be non-negative")
    if snapshot.last_update_block > snapshot.block:
        raise ValueError("last update exceeds finalized block")
    if any(commit.commit_block > snapshot.block for commit in snapshot.commitments):
        raise ValueError("commitment block exceeds finalized block")
    if any(reveal.reveal_block > snapshot.block for reveal in snapshot.reveals):
        raise ValueError("reveal block exceeds finalized block")
    hotkey_uids = tuple(uid for uid, _hotkey in snapshot.hotkeys)
    if len(hotkey_uids) != len(set(hotkey_uids)):
        raise ValueError("finalized hotkey UIDs must be unique")
    weight_uids = tuple(uid for uid, _weight in snapshot.weights)
    if len(weight_uids) != len(set(weight_uids)):
        raise ValueError("finalized weight UIDs must be unique")


def _cr4_weight_resolution(
    *,
    snapshot: WeightEmissionChainSnapshot,
    submission_block: int,
    baseline_block: int,
    period_blocks: int,
    deadline: int | None,
    reveal_deadline: int | None,
    finality_margin_blocks: int,
    commitment_hash: str | None,
    reveal_round: int | None,
    observed_block: int | None,
    observed_last_update_block: int | None,
    vector_matches: bool,
    reveal_scan_complete: bool,
) -> WeightResolutionDecision:
    inclusion_deadline = (
        deadline - finality_margin_blocks
        if isinstance(deadline, int)
        else submission_block + period_blocks
    )
    candidates = tuple(
        commit
        for commit in snapshot.commitments
        if commit.validator_hotkey == snapshot.validator_hotkey
        and submission_block <= commit.commit_block <= inclusion_deadline
        and (
            (commitment_hash is None and reveal_round is None)
            or (
                commit.commitment_hash == commitment_hash
                and commit.reveal_round == reveal_round
            )
        )
    )
    if len(candidates) > 1:
        return WeightResolutionDecision(
            unconfirm=isinstance(deadline, int) and snapshot.block > deadline
        )
    matching_commit = candidates[0] if len(candidates) == 1 else None
    reveal_matches = any(
        reveal.validator_hotkey == snapshot.validator_hotkey
        and reveal.netuid == snapshot.netuid
        and (observed_block if isinstance(observed_block, int) else submission_block)
        <= reveal.reveal_block
        and (
            not isinstance(reveal_deadline, int)
            or reveal.reveal_block <= reveal_deadline
        )
        for reveal in snapshot.reveals
    )
    confirmed = reveal_matches and vector_matches
    if confirmed:
        return WeightResolutionDecision(confirm=True)
    if matching_commit is not None and observed_block is None:
        return WeightResolutionDecision(
            observed_commit_block=matching_commit.commit_block,
            observed_last_update_block=snapshot.last_update_block,
            observed_commitment_hash=(
                matching_commit.commitment_hash if commitment_hash is None else None
            ),
            observed_reveal_round=(
                matching_commit.reveal_round if reveal_round is None else None
            ),
        )
    if isinstance(observed_block, int) and isinstance(observed_last_update_block, int):
        expiry_deadline = (
            reveal_deadline if isinstance(reveal_deadline, int) else deadline
        )
        commit_pending = matching_commit is not None
        return WeightResolutionDecision(
            unconfirm=(
                reveal_scan_complete
                and not commit_pending
                and isinstance(expiry_deadline, int)
                and snapshot.block > expiry_deadline
            ),
        )
    expiry_deadline = reveal_deadline if isinstance(reveal_deadline, int) else deadline
    return WeightResolutionDecision(
        unconfirm=(
            reveal_scan_complete
            and isinstance(expiry_deadline, int)
            and snapshot.block > expiry_deadline
        ),
    )


def _expected_weight_targets(
    connection: Connection, batch_id: int
) -> tuple[tuple[int, str, int], ...]:
    return tuple(
        (
            int(row._mapping["uid"]),
            str(row._mapping["miner_hotkey"]),
            int(row._mapping["weight_u16"]),
        )
        for row in connection.execute(
            select(
                weight_emission_rows.c.uid,
                weight_emission_rows.c.miner_hotkey,
                weight_emission_rows.c.weight_u16,
            )
            .where(
                weight_emission_rows.c.batch_id == batch_id,
                weight_emission_rows.c.weight_u16.is_not(None),
            )
            .order_by(weight_emission_rows.c.uid)
        )
    )


def _apply_weight_resolution(
    connection: Connection,
    *,
    batch_id: int,
    confirmation_state: str,
    decision: WeightResolutionDecision,
    confirmed_at_iso: str,
) -> tuple[int, int]:
    if decision.observed_commit_block is not None:
        connection.execute(
            update(weight_emission_batches)
            .where(
                weight_emission_batches.c.id == batch_id,
                weight_emission_batches.c.confirmation_state == confirmation_state,
            )
            .values(
                commitment_observed_block=decision.observed_commit_block,
                commitment_observed_last_update_block=(
                    decision.observed_last_update_block
                ),
                commitment_hash=func.coalesce(
                    decision.observed_commitment_hash,
                    weight_emission_batches.c.commitment_hash,
                ),
                reveal_round=func.coalesce(
                    decision.observed_reveal_round,
                    weight_emission_batches.c.reveal_round,
                ),
            )
        )
        return 0, 0
    if decision.confirm:
        result = connection.execute(
            update(weight_emission_batches)
            .where(
                weight_emission_batches.c.id == batch_id,
                weight_emission_batches.c.confirmation_state == confirmation_state,
            )
            .values(
                confirmation_state="confirmed",
                confirmed_at=confirmed_at_iso,
            )
        )
        if result.rowcount != 1:
            return 0, 0
        connection.execute(
            update(weight_emission_rows)
            .where(
                weight_emission_rows.c.batch_id == batch_id,
                weight_emission_rows.c.weight_u16.is_not(None),
            )
            .values(emitted=True)
        )
        return 1, 0
    if not decision.unconfirm:
        return 0, 0
    result = connection.execute(
        update(weight_emission_batches)
        .where(
            weight_emission_batches.c.id == batch_id,
            weight_emission_batches.c.confirmation_state == confirmation_state,
        )
        .values(confirmation_state="unconfirmed")
    )
    return (0, 1) if result.rowcount == 1 else (0, 0)


def _coordinate_values(coordinate: AssessmentCoordinate) -> dict[str, object]:
    return {
        "target_kind": coordinate.target_kind,
        "target_id": coordinate.target_id,
        "horizon_kind": coordinate.horizon_kind,
        "horizon_value": coordinate.horizon_value,
        "output": coordinate.output,
    }


def _text_from_mapping(row: RowMapping, column: str) -> str:
    value = row[column]
    if not isinstance(value, str):
        raise TypeError(f"{column} must be text")
    return value


def _int_from_mapping(row: RowMapping, column: str) -> int:
    value = row[column]
    if not isinstance(value, int):
        raise TypeError(f"{column} must be integer")
    return value


def _decimal_from_mapping(row: RowMapping, column: str) -> Decimal:
    return Decimal(_text_from_mapping(row, column))


def _optional_decimal_from_mapping(row: RowMapping, column: str) -> Decimal | None:
    value = row[column]
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{column} must be text or null")
    return Decimal(value)


def _optional_text_from_mapping(row: RowMapping, column: str) -> str | None:
    value = row[column]
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{column} must be text or null")
    return value


def _target_kind_from_mapping(row: RowMapping) -> TargetKind:
    value = _text_from_mapping(row, "target_kind")
    if value == "equity_ticker":
        return "equity_ticker"
    if value == "subnet_asset":
        return "subnet_asset"
    raise ValueError(f"unknown target_kind: {value}")


def _horizon_kind_from_mapping(row: RowMapping) -> HorizonKind:
    value = _text_from_mapping(row, "horizon_kind")
    if value == "trading_days":
        return "trading_days"
    if value == "seconds":
        return "seconds"
    raise ValueError(f"unknown horizon_kind: {value}")


def _coordinate_from_mapping(row: RowMapping) -> AssessmentCoordinate:
    return AssessmentCoordinate(
        target_kind=_target_kind_from_mapping(row),
        target_id=_text_from_mapping(row, "target_id"),
        horizon_kind=_horizon_kind_from_mapping(row),
        horizon_value=_int_from_mapping(row, "horizon_value"),
        output=_text_from_mapping(row, "output"),
    )


def _apply_sqlite_pragmas(engine: Engine) -> None:
    """Production hardening for the embedded store: WAL keeps the axon, tick,
    and API threads from blocking each other; the busy timeout absorbs writer
    contention; synchronous=NORMAL is the WAL-safe durability point; foreign
    keys are otherwise decorative on SQLite."""

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


class Storage:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, url: str) -> Storage:
        # SQLite-only by design: the store leans on the WAL/foreign-key pragmas
        # and SQLite's text-typed Decimal storage. Fail fast on any other
        # backend rather than running with the pragmas silently skipped. The
        # backend is read from the URL (make_url) so no DBAPI driver is needed.
        backend = make_url(url).get_backend_name()
        if backend != "sqlite":
            raise ValueError(
                f"Storage supports SQLite only, got backend {backend!r} from URL"
            )
        engine = create_engine(url)
        _apply_sqlite_pragmas(engine)
        return cls(engine)

    # -- rounds ----------------------------------------------------------

    def open_round(
        self,
        *,
        windows: RoundWindows,
        schema_id: str,
        universe: UniverseSnapshot | None,
        now_iso: str,
    ) -> None:
        """Create the round row + universe snapshot; idempotent."""
        with self._engine.begin() as connection:
            existing = connection.execute(
                select(rounds.c.state).where(
                    rounds.c.round_id == windows.round_id,
                    rounds.c.schema_id == schema_id,
                )
            ).first()
            if existing is not None:
                return
            if universe is not None:
                connection.execute(
                    insert(universe_snapshots).values(
                        round_id=windows.round_id,
                        schema_id=schema_id,
                        tickers_json=json.dumps(list(universe.tickers)),
                        source_hash=universe.source_hash,
                        fetched_at=now_iso,
                    )
                )
            connection.execute(
                insert(rounds).values(
                    round_id=windows.round_id,
                    schema_id=schema_id,
                    state=ROUND_STATE_OPEN,
                    universe_stale=universe is None,
                    degraded=False,
                    commit_open_at=windows.commit_open.isoformat(),
                    commit_close_at=windows.commit_close.isoformat(),
                    reveal_open_at=windows.reveal_open.isoformat(),
                    reveal_close_at=windows.reveal_close.isoformat(),
                    t0_close_at=windows.t0_close.isoformat(),
                    created_at=now_iso,
                    updated_at=now_iso,
                )
            )

    def round_windows(self, round_id: str, schema_id: str) -> RoundWindows | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(
                    rounds.c.commit_open_at,
                    rounds.c.commit_close_at,
                    rounds.c.t0_close_at,
                    rounds.c.reveal_open_at,
                    rounds.c.reveal_close_at,
                ).where(
                    rounds.c.round_id == round_id,
                    rounds.c.schema_id == schema_id,
                )
            ).first()
        if row is None:
            return None
        return RoundWindows(
            round_id=round_id,
            commit_open=datetime.fromisoformat(row.commit_open_at),
            commit_close=datetime.fromisoformat(row.commit_close_at),
            t0_close=datetime.fromisoformat(row.t0_close_at),
            reveal_open=datetime.fromisoformat(row.reveal_open_at),
            reveal_close=datetime.fromisoformat(row.reveal_close_at),
        )

    def unfinished_rounds(self, schema_id: str) -> list[str]:
        """Round ids not yet closed, oldest first."""
        with self._engine.connect() as connection:
            result = connection.execute(
                select(rounds.c.round_id)
                .where(
                    rounds.c.schema_id == schema_id,
                    rounds.c.state != ROUND_STATE_CLOSED,
                )
                .order_by(rounds.c.round_id)
            )
            return [row.round_id for row in result]

    def unfinished_assessment_resolution_progress(
        self, schema_id: str
    ) -> list[AssessmentRoundResolutionProgress]:
        """Resolution markers and deadlines for unfinished assessment rounds."""
        marker = assessment_horizon_resolutions.alias("health_marker")
        query = (
            select(
                rounds.c.round_id,
                rounds.c.reveal_close_at,
                marker.c.horizon_value,
            )
            .select_from(
                rounds.outerjoin(
                    marker,
                    and_(
                        marker.c.round_id == rounds.c.round_id,
                        marker.c.schema_id == rounds.c.schema_id,
                    ),
                )
            )
            .where(
                rounds.c.schema_id == schema_id,
                rounds.c.state != ROUND_STATE_CLOSED,
            )
            .order_by(rounds.c.round_id, marker.c.horizon_value)
        )
        resolved_by_round: dict[str, set[int]] = {}
        reveal_close_by_round: dict[str, datetime] = {}
        with self._engine.connect() as connection:
            for row in connection.execute(query):
                round_id = str(row.round_id)
                reveal_close_by_round[round_id] = datetime.fromisoformat(
                    row.reveal_close_at
                )
                resolved = resolved_by_round.setdefault(round_id, set())
                if row.horizon_value is not None:
                    resolved.add(int(row.horizon_value))
        return [
            AssessmentRoundResolutionProgress(
                round_id=round_id,
                reveal_close_at=reveal_close_by_round[round_id],
                resolved_horizons=frozenset(resolved_horizons),
            )
            for round_id, resolved_horizons in resolved_by_round.items()
        ]

    def list_rounds(
        self,
        schema_id: str,
        *,
        state: str | None = None,
        limit: int = 50,
        before: str | None = None,
    ) -> list[dict[str, object]]:
        """Round index for the dashboard surface, newest first."""
        query = select(
            rounds.c.round_id,
            rounds.c.state,
            rounds.c.created_at,
            rounds.c.reveal_close_at,
        ).where(rounds.c.schema_id == schema_id)
        if state is not None:
            query = query.where(rounds.c.state == state)
        if before is not None:
            query = query.where(rounds.c.round_id < before)
        query = query.order_by(rounds.c.round_id.desc()).limit(limit)
        with self._engine.connect() as connection:
            return [
                {
                    "round_id": row.round_id,
                    "state": row.state,
                    "created_at": row.created_at,
                    "reveal_close_at": row.reveal_close_at,
                }
                for row in connection.execute(query)
            ]

    def _post_embargo_rounds(self, schema_id: str):
        """Embargo enforced at the read path, not by write-ordering trust."""
        return select(rounds.c.round_id).where(
            rounds.c.schema_id == schema_id,
            rounds.c.state.in_(POST_EMBARGO_ROUND_STATES),
        )

    def round_state(self, round_id: str, schema_id: str) -> str | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(rounds.c.state).where(
                    rounds.c.round_id == round_id,
                    rounds.c.schema_id == schema_id,
                )
            ).first()
        return None if row is None else row.state

    def set_round_state(
        self, round_id: str, schema_id: str, state: str, *, now_iso: str
    ) -> None:
        if state not in VALID_ROUND_STATES:
            raise ValueError(f"invalid round state: {state}")
        with self._engine.begin() as connection:
            if state == ROUND_STATE_REVEALED:
                self._snapshot_consensus_bundles_if_open(
                    connection, round_id, schema_id, now_iso=now_iso
                )
            connection.execute(
                update(rounds)
                .where(
                    rounds.c.round_id == round_id,
                    rounds.c.schema_id == schema_id,
                )
                .values(state=state, updated_at=now_iso)
            )

    def universe_for(self, round_id: str, schema_id: str) -> UniverseSnapshot | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(
                    universe_snapshots.c.tickers_json,
                    universe_snapshots.c.source_hash,
                ).where(
                    universe_snapshots.c.round_id == round_id,
                    universe_snapshots.c.schema_id == schema_id,
                )
            ).first()
        if row is None:
            return None
        return UniverseSnapshot(
            round_id=round_id,
            tickers=tuple(json.loads(row.tickers_json)),
            source_hash=row.source_hash,
        )

    # -- commits ---------------------------------------------------------

    def record_commit(
        self,
        round_id: str,
        schema_id: str,
        miner_hotkey: str,
        bundle_hash: str,
        *,
        now_iso: str,
        max_commits: int | None = None,
    ) -> bool:
        """Idempotent last-wins commit upsert, rate-capped atomically.

        An unchanged hash is an accepted no-op. Changed hashes check and
        increment in one transaction: the UPDATE only fires while
        commit_count < max_commits, so concurrent commits cannot exceed the
        cap. Returns False when a changed hash is rate-limited.
        """
        with self._engine.begin() as connection:
            update_query = (
                update(submissions)
                .where(
                    submissions.c.round_id == round_id,
                    submissions.c.schema_id == schema_id,
                    submissions.c.miner_hotkey == miner_hotkey,
                    submissions.c.commit_hash.is_distinct_from(bundle_hash),
                )
                .values(
                    commit_hash=bundle_hash,
                    committed_at=now_iso,
                    commit_count=submissions.c.commit_count + 1,
                    verdict="committed",
                )
            )
            if max_commits is not None:
                update_query = update_query.where(
                    submissions.c.commit_count < max_commits
                )
            result = connection.execute(update_query)
            if result.rowcount > 0:
                return True
            existing = connection.execute(
                select(submissions.c.commit_hash).where(
                    submissions.c.round_id == round_id,
                    submissions.c.schema_id == schema_id,
                    submissions.c.miner_hotkey == miner_hotkey,
                )
            ).first()
            if existing is not None:
                return existing.commit_hash == bundle_hash
            created = connection.execute(
                sqlite_insert(submissions)
                .values(
                    round_id=round_id,
                    schema_id=schema_id,
                    miner_hotkey=miner_hotkey,
                    commit_hash=bundle_hash,
                    committed_at=now_iso,
                    commit_count=1,
                    verdict="committed",
                )
                .on_conflict_do_nothing()
            )
            if created.rowcount > 0:
                return True
            # A concurrent first commit won the insert race — retry the
            # capped update against the row it created. Its hash may be
            # identical, which is a successful no-op rather than a rate hit.
            if connection.execute(update_query).rowcount > 0:
                return True
            concurrent = connection.execute(
                select(submissions.c.commit_hash).where(
                    submissions.c.round_id == round_id,
                    submissions.c.schema_id == schema_id,
                    submissions.c.miner_hotkey == miner_hotkey,
                )
            ).first()
            return concurrent is not None and concurrent.commit_hash == bundle_hash

    def committed_hash(
        self, round_id: str, schema_id: str, miner_hotkey: str
    ) -> str | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(submissions.c.commit_hash).where(
                    submissions.c.round_id == round_id,
                    submissions.c.schema_id == schema_id,
                    submissions.c.miner_hotkey == miner_hotkey,
                )
            ).first()
        return None if row is None else row.commit_hash

    def commit_count(self, round_id: str, schema_id: str, miner_hotkey: str) -> int:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(submissions.c.commit_count).where(
                    submissions.c.round_id == round_id,
                    submissions.c.schema_id == schema_id,
                    submissions.c.miner_hotkey == miner_hotkey,
                )
            ).first()
        return 0 if row is None else int(row.commit_count)

    # -- reveals ---------------------------------------------------------

    def record_reveal_attempt(
        self,
        round_id: str,
        schema_id: str,
        miner_hotkey: str,
        *,
        max_reveals: int | None = None,
    ) -> bool:
        """Atomically charge one reveal attempt against a committed miner.

        The counter lives beside the commit and survives validator restarts.
        An absent commit cannot spend reveal budget because it cannot produce a
        valid reveal and has no submission row to charge.
        """
        with self._engine.begin() as connection:
            update_query = update(submissions).where(
                submissions.c.round_id == round_id,
                submissions.c.schema_id == schema_id,
                submissions.c.miner_hotkey == miner_hotkey,
                submissions.c.commit_hash.is_not(None),
            )
            if max_reveals is not None:
                update_query = update_query.where(
                    submissions.c.reveal_count < max_reveals
                )
            result = connection.execute(
                update_query.values(
                    reveal_count=submissions.c.reveal_count + 1,
                )
            )
            return result.rowcount > 0

    def reveal_count(self, round_id: str, schema_id: str, miner_hotkey: str) -> int:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(submissions.c.reveal_count).where(
                    submissions.c.round_id == round_id,
                    submissions.c.schema_id == schema_id,
                    submissions.c.miner_hotkey == miner_hotkey,
                )
            ).first()
        return 0 if row is None else int(row.reveal_count)

    def accepted_reveal(
        self, round_id: str, schema_id: str, miner_hotkey: str
    ) -> tuple[str, str] | None:
        """Return the accepted bundle and nonce for an idempotent retry."""
        with self._engine.connect() as connection:
            row = connection.execute(
                select(submissions.c.bundle_json, submissions.c.nonce_hex).where(
                    submissions.c.round_id == round_id,
                    submissions.c.schema_id == schema_id,
                    submissions.c.miner_hotkey == miner_hotkey,
                    submissions.c.verdict == "accepted",
                )
            ).first()
        if row is None or row.bundle_json is None or row.nonce_hex is None:
            return None
        return str(row.bundle_json), str(row.nonce_hex)

    def record_reveal(
        self,
        round_id: str,
        schema_id: str,
        miner_hotkey: str,
        *,
        bundle_json: str,
        nonce_hex: str,
        accepted: bool,
        rejection_code: str | None,
        now_iso: str,
    ) -> bool:
        """Upsert a reveal verdict; returns whether this call persisted its values.

        A repeated identical reveal (the miner's per-tick re-push of the same
        bundle while it waits on the window/acks, or a hostile replay) is a
        no-op write — record_reveal is idempotent so a reveal can't churn the
        row. An accepted reveal is terminal: a later rejected attempt never
        erases a bundle that 5d/30d scoring still has to read.
        """
        verdict = "accepted" if accepted else "rejected"
        values = {
            "revealed_at": now_iso,
            "bundle_json": bundle_json,
            "nonce_hex": nonce_hex,
            "verdict": verdict,
            "rejection_code": rejection_code,
        }
        with self._engine.begin() as connection:
            reveal_row = select(
                submissions.c.verdict,
                submissions.c.bundle_json,
                submissions.c.nonce_hex,
                submissions.c.rejection_code,
            ).where(
                submissions.c.round_id == round_id,
                submissions.c.schema_id == schema_id,
                submissions.c.miner_hotkey == miner_hotkey,
            )
            existing = connection.execute(reveal_row).first()
            if existing is None:
                created = connection.execute(
                    sqlite_insert(submissions)
                    .values(
                        round_id=round_id,
                        schema_id=schema_id,
                        miner_hotkey=miner_hotkey,
                        **values,
                    )
                    .on_conflict_do_nothing()
                )
                if created.rowcount > 0:
                    return True
                # The first SELECT holds no write lock under deferred WAL
                # locking, so another first reveal can win the insert gap. A
                # no-op conflict did not persist this call's values; re-read
                # the authoritative row before reconciling it below.
                existing = connection.execute(reveal_row).first()
            if existing is None:
                return False
            if not accepted and existing.verdict == "accepted":
                return False  # accepted is terminal
            if (
                existing.verdict == verdict
                and existing.bundle_json == bundle_json
                and existing.nonce_hex == nonce_hex
                and existing.rejection_code == rejection_code
            ):
                return False  # identical re-push — no-op
            connection.execute(
                update(submissions)
                .where(
                    submissions.c.round_id == round_id,
                    submissions.c.schema_id == schema_id,
                    submissions.c.miner_hotkey == miner_hotkey,
                )
                .values(**values)
            )
            return True

    # -- consensus ---------------------------------------------------------

    @staticmethod
    def _accepted_bundles_from_connection(
        connection: Connection, round_id: str, schema_id: str
    ) -> list[tuple[str, str]]:
        result = connection.execute(
            select(submissions.c.miner_hotkey, submissions.c.bundle_json)
            .where(
                submissions.c.round_id == round_id,
                submissions.c.schema_id == schema_id,
                submissions.c.verdict == "accepted",
            )
            .order_by(submissions.c.miner_hotkey)
        )
        return [(row.miner_hotkey, row.bundle_json) for row in result]

    @staticmethod
    def _consensus_bundles_from_connection(
        connection: Connection, round_id: str, schema_id: str
    ) -> list[tuple[str, str]]:
        result = connection.execute(
            select(
                consensus_bundle_snapshots.c.miner_hotkey,
                consensus_bundle_snapshots.c.bundle_json,
            )
            .where(
                consensus_bundle_snapshots.c.round_id == round_id,
                consensus_bundle_snapshots.c.schema_id == schema_id,
            )
            .order_by(consensus_bundle_snapshots.c.miner_hotkey)
        )
        return [(row.miner_hotkey, row.bundle_json) for row in result]

    def _snapshot_consensus_bundles_if_open(
        self,
        connection: Connection,
        round_id: str,
        schema_id: str,
        *,
        now_iso: str,
    ) -> list[tuple[str, str]]:
        state = connection.execute(
            select(rounds.c.state).where(
                rounds.c.round_id == round_id,
                rounds.c.schema_id == schema_id,
            )
        ).scalar_one_or_none()
        if state != ROUND_STATE_OPEN:
            return self._consensus_bundles_from_connection(
                connection, round_id, schema_id
            )
        snapshots = self._consensus_bundles_from_connection(
            connection, round_id, schema_id
        )
        if snapshots:
            return snapshots
        bundles = self._accepted_bundles_from_connection(
            connection, round_id, schema_id
        )
        for miner_hotkey, bundle_json in bundles:
            connection.execute(
                sqlite_insert(consensus_bundle_snapshots)
                .values(
                    round_id=round_id,
                    schema_id=schema_id,
                    miner_hotkey=miner_hotkey,
                    bundle_json=bundle_json,
                    snapshotted_at=now_iso,
                )
                .on_conflict_do_nothing()
            )
        return self._consensus_bundles_from_connection(connection, round_id, schema_id)

    @staticmethod
    def _flip_round_revealed(
        connection: Connection, round_id: str, schema_id: str, *, now_iso: str
    ) -> None:
        """Flip an OPEN round to 'revealed'; never rewinds a scored round.

        Shared by every publish path so the reveal
        transition has one definition. The 'open' predicate makes a late or
        replayed publish a no-op against a round that scoring already advanced,
        while a zero-submission round (still open, no rows) reveals normally.
        """
        connection.execute(
            update(rounds)
            .where(
                rounds.c.round_id == round_id,
                rounds.c.schema_id == schema_id,
                rounds.c.state == ROUND_STATE_OPEN,
            )
            .values(state=ROUND_STATE_REVEALED, updated_at=now_iso)
        )

    def round_meta(self, round_id: str, schema_id: str) -> dict[str, object] | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(rounds).where(
                    rounds.c.round_id == round_id,
                    rounds.c.schema_id == schema_id,
                )
            ).first()
            if row is None:
                return None
            accepted = connection.execute(
                select(func.count(submissions.c.id)).where(
                    submissions.c.round_id == round_id,
                    submissions.c.schema_id == schema_id,
                    submissions.c.verdict == "accepted",
                )
            ).scalar_one()
        universe = self.universe_for(round_id, schema_id)
        return {
            "round_id": round_id,
            "schema_id": schema_id,
            "state": row.state,
            "created_at": row.created_at,
            "universe_stale": bool(row.universe_stale),
            "degraded": bool(row.degraded),
            "commit_open_at": row.commit_open_at,
            "commit_close_at": row.commit_close_at,
            "t0_close_at": row.t0_close_at,
            "reveal_open_at": row.reveal_open_at,
            "reveal_close_at": row.reveal_close_at,
            "universe_source_hash": universe.source_hash if universe else None,
            "universe_size": len(universe.tickers) if universe else 0,
            "accepted_submissions": accepted,
        }

    def accepted_bundles(
        self,
        round_id: str,
        schema_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[tuple[str, str]]:
        """Accepted (hotkey, bundle_json) for a round, ordered by hotkey.

        ``limit`` is optional so internal callers (consensus, scoring) read the
        whole round; the public ``/submissions`` endpoint passes a bound to
        page the response.
        """
        query = (
            select(submissions.c.miner_hotkey, submissions.c.bundle_json)
            .where(
                submissions.c.round_id == round_id,
                submissions.c.schema_id == schema_id,
                submissions.c.verdict == "accepted",
            )
            .order_by(submissions.c.miner_hotkey)
        )
        if limit is not None:
            query = query.limit(limit).offset(max(0, offset))
        with self._engine.connect() as connection:
            result = connection.execute(query)
            return [(row.miner_hotkey, row.bundle_json) for row in result]

    def _snapshot_legacy_consensus_bundles_if_absent(
        self,
        connection: Connection,
        round_id: str,
        schema_id: str,
        *,
        now_iso: str,
    ) -> list[tuple[str, str]]:
        snapshots = self._consensus_bundles_from_connection(
            connection, round_id, schema_id
        )
        if snapshots:
            return snapshots
        bundles = self._accepted_bundles_from_connection(
            connection, round_id, schema_id
        )
        for miner_hotkey, bundle_json in bundles:
            connection.execute(
                sqlite_insert(consensus_bundle_snapshots)
                .values(
                    round_id=round_id,
                    schema_id=schema_id,
                    miner_hotkey=miner_hotkey,
                    bundle_json=bundle_json,
                    snapshotted_at=now_iso,
                )
                .on_conflict_do_nothing()
            )
        return self._consensus_bundles_from_connection(connection, round_id, schema_id)

    def scoring_bundles(
        self,
        round_id: str,
        schema_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
        now_iso: str | None = None,
    ) -> list[tuple[str, str]]:
        """Return consensus snapshots after reveal and live accepted bundles before it."""
        with self._engine.begin() as connection:
            state = connection.execute(
                select(rounds.c.state).where(
                    rounds.c.round_id == round_id,
                    rounds.c.schema_id == schema_id,
                )
            ).scalar_one_or_none()
            if state in POST_EMBARGO_ROUND_STATES:
                snapshots = self._consensus_bundles_from_connection(
                    connection, round_id, schema_id
                )
                bundles = (
                    snapshots
                    if snapshots
                    else self._snapshot_legacy_consensus_bundles_if_absent(
                        connection,
                        round_id,
                        schema_id,
                        now_iso=(
                            now_iso
                            if now_iso is not None
                            else datetime.now(UTC).isoformat()
                        ),
                    )
                )
            else:
                bundles = self._accepted_bundles_from_connection(
                    connection, round_id, schema_id
                )
        start = max(0, offset)
        return bundles[start:] if limit is None else bundles[start : start + limit]

    # -- generic assessment-coordinate storage ----------------------------

    def accepted_assessment_bundles(
        self,
        round_id: str,
        schema_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AcceptedAssessmentBundle]:
        """Accepted reveal payloads for the schema-neutral storage contract."""
        return [
            AcceptedAssessmentBundle(miner_hotkey=hotkey, bundle_json=bundle_json)
            for hotkey, bundle_json in self.scoring_bundles(
                round_id, schema_id, limit=limit, offset=offset
            )
        ]

    def _insert_assessment_consensus_if_absent(
        self,
        connection: Connection,
        round_id: str,
        schema_id: str,
        rows: Sequence[AssessmentConsensusRow],
        *,
        now_iso: str,
    ) -> None:
        """Insert generic consensus rows once; recomputation never overwrites.

        Guarded at round granularity (not per cell) so a recomputation over a
        changed coordinate set cannot interleave new cells with a prior run's.
        """
        existing = connection.execute(
            select(assessment_consensus.c.id)
            .where(
                assessment_consensus.c.round_id == round_id,
                assessment_consensus.c.schema_id == schema_id,
            )
            .limit(1)
        ).first()
        if existing is not None:
            return
        for row in rows:
            connection.execute(
                insert(assessment_consensus).values(
                    round_id=round_id,
                    schema_id=schema_id,
                    **_coordinate_values(row.coordinate),
                    value_text=str(row.value),
                    dispersion_text=str(row.dispersion),
                    n_submitters=row.n_submitters,
                    computed_at=now_iso,
                )
            )

    def publish_assessment_consensus_and_reveal(
        self,
        round_id: str,
        schema_id: str,
        rows: Sequence[AssessmentConsensusRow],
        *,
        now_iso: str,
    ) -> None:
        """Persist generic consensus rows and flip to 'revealed' atomically.

        The generic-coordinate analog of ``publish_consensus_and_reveal``: the
        two writes share one transaction so a crash can never leave a lending
        round reading 'open' with consensus already on disk. Row insertion is
        round-idempotent, and the flip only ever moves an open round forward.
        This is the ONLY generic-consensus writer — no non-atomic sibling
        exists, so consensus can never land without the reveal transition.
        """
        with self._engine.begin() as connection:
            self._snapshot_consensus_bundles_if_open(
                connection, round_id, schema_id, now_iso=now_iso
            )
            self._insert_assessment_consensus_if_absent(
                connection, round_id, schema_id, rows, now_iso=now_iso
            )
            self._flip_round_revealed(connection, round_id, schema_id, now_iso=now_iso)

    def publish_assessment_consensus_from_accepted_bundles_and_reveal(
        self,
        round_id: str,
        schema_id: str,
        consensus_rows: Callable[[list[tuple[str, str]]], list[AssessmentConsensusRow]],
        *,
        now_iso: str,
    ) -> tuple[list[tuple[str, str]], list[AssessmentConsensusRow]]:
        """Build assessment consensus from and persist one atomic bundle snapshot."""
        with self._engine.begin() as connection:
            bundles = self._snapshot_consensus_bundles_if_open(
                connection, round_id, schema_id, now_iso=now_iso
            )
            rows = consensus_rows(bundles)
            self._insert_assessment_consensus_if_absent(
                connection, round_id, schema_id, rows, now_iso=now_iso
            )
            self._flip_round_revealed(connection, round_id, schema_id, now_iso=now_iso)
        return bundles, rows

    def assessment_consensus_for(
        self, round_id: str, schema_id: str
    ) -> list[AssessmentConsensusRow]:
        with self._engine.connect() as connection:
            result = connection.execute(
                select(
                    assessment_consensus.c.target_kind,
                    assessment_consensus.c.target_id,
                    assessment_consensus.c.horizon_kind,
                    assessment_consensus.c.horizon_value,
                    assessment_consensus.c.output,
                    assessment_consensus.c.value_text,
                    assessment_consensus.c.dispersion_text,
                    assessment_consensus.c.n_submitters,
                )
                .where(
                    assessment_consensus.c.round_id == round_id,
                    assessment_consensus.c.schema_id == schema_id,
                )
                .order_by(
                    assessment_consensus.c.target_kind,
                    assessment_consensus.c.target_id,
                    assessment_consensus.c.horizon_kind,
                    assessment_consensus.c.horizon_value,
                    assessment_consensus.c.output,
                )
            )
            return [
                AssessmentConsensusRow(
                    coordinate=_coordinate_from_mapping(row._mapping),
                    value=_decimal_from_mapping(row._mapping, "value_text"),
                    dispersion=_decimal_from_mapping(row._mapping, "dispersion_text"),
                    n_submitters=_int_from_mapping(row._mapping, "n_submitters"),
                )
                for row in result
            ]

    def latest_assessment_consensus_round(self, schema_id: str) -> str | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(assessment_consensus.c.round_id)
                .distinct()
                .where(
                    assessment_consensus.c.schema_id == schema_id,
                    assessment_consensus.c.round_id.in_(
                        self._post_embargo_rounds(schema_id)
                    ),
                )
                # Round ids are ISO dates (YYYY-MM-DD), so lexical DESC is newest first.
                .order_by(assessment_consensus.c.round_id.desc())
                .limit(1)
            ).first()
        return None if row is None else row.round_id

    def latest_assessment_rounds_with_resolved_outputs(
        self,
        schema_id: str,
        horizon_value: int,
        outputs: Sequence[str],
    ) -> dict[int, str]:
        """Latest complete resolved round per subnet netuid for one horizon."""
        required_outputs = frozenset(outputs)
        if not required_outputs:
            return {}
        complete_target_rounds = (
            select(
                assessment_realized_targets.c.target_id,
                assessment_realized_targets.c.round_id,
            )
            .select_from(
                assessment_realized_targets.join(
                    assessment_consensus,
                    and_(
                        assessment_consensus.c.round_id
                        == assessment_realized_targets.c.round_id,
                        assessment_consensus.c.schema_id
                        == assessment_realized_targets.c.schema_id,
                        assessment_consensus.c.target_kind
                        == assessment_realized_targets.c.target_kind,
                        assessment_consensus.c.target_id
                        == assessment_realized_targets.c.target_id,
                        assessment_consensus.c.horizon_kind
                        == assessment_realized_targets.c.horizon_kind,
                        assessment_consensus.c.horizon_value
                        == assessment_realized_targets.c.horizon_value,
                        assessment_consensus.c.output
                        == assessment_realized_targets.c.output,
                    ),
                )
            )
            .where(
                assessment_realized_targets.c.schema_id == schema_id,
                assessment_realized_targets.c.horizon_value == horizon_value,
                assessment_realized_targets.c.target_kind == "subnet_asset",
                assessment_realized_targets.c.status == "resolved",
                assessment_realized_targets.c.value_text.is_not(None),
                assessment_realized_targets.c.output.in_(tuple(required_outputs)),
                assessment_realized_targets.c.round_id.in_(
                    self._post_embargo_rounds(schema_id)
                ),
            )
            .group_by(
                assessment_realized_targets.c.target_id,
                assessment_realized_targets.c.round_id,
            )
            .having(
                func.count(func.distinct(assessment_realized_targets.c.output))
                == len(required_outputs)
            )
            .subquery()
        )
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    complete_target_rounds.c.target_id,
                    # Round ids are ISO dates (YYYY-MM-DD), so MAX selects newest.
                    func.max(complete_target_rounds.c.round_id).label("round_id"),
                )
                .group_by(complete_target_rounds.c.target_id)
                .order_by(complete_target_rounds.c.target_id)
            ).all()
        return {int(row.target_id): row.round_id for row in rows}

    @staticmethod
    def _insert_assessment_realized_targets(
        connection: Connection,
        round_id: str,
        schema_id: str,
        rows: Sequence[AssessmentRealizedTarget],
        *,
        now_iso: str,
    ) -> None:
        for row in rows:
            connection.execute(
                sqlite_insert(assessment_realized_targets)
                .values(
                    round_id=round_id,
                    schema_id=schema_id,
                    **_coordinate_values(row.coordinate),
                    value_text=None if row.value is None else str(row.value),
                    status=row.status,
                    provider_payload_hash=row.provider_payload_hash,
                    fetched_at=now_iso,
                )
                .on_conflict_do_nothing()
            )

    def record_assessment_realized_targets(
        self,
        round_id: str,
        schema_id: str,
        rows: Sequence[AssessmentRealizedTarget],
        *,
        now_iso: str,
    ) -> None:
        """Persist realized targets once; first observation wins."""
        with self._engine.begin() as connection:
            self._insert_assessment_realized_targets(
                connection, round_id, schema_id, rows, now_iso=now_iso
            )

    def assessment_realized_targets_for(
        self, round_id: str, schema_id: str
    ) -> list[AssessmentRealizedTarget]:
        return self.assessment_realized_targets_for_horizon(round_id, schema_id, None)

    def assessment_realized_targets_for_horizon(
        self, round_id: str, schema_id: str, horizon_value: int | None
    ) -> list[AssessmentRealizedTarget]:
        with self._engine.connect() as connection:
            query = select(
                assessment_realized_targets.c.target_kind,
                assessment_realized_targets.c.target_id,
                assessment_realized_targets.c.horizon_kind,
                assessment_realized_targets.c.horizon_value,
                assessment_realized_targets.c.output,
                assessment_realized_targets.c.value_text,
                assessment_realized_targets.c.status,
                assessment_realized_targets.c.provider_payload_hash,
            ).where(
                assessment_realized_targets.c.round_id == round_id,
                assessment_realized_targets.c.schema_id == schema_id,
            )
            if horizon_value is not None:
                query = query.where(
                    assessment_realized_targets.c.horizon_value == horizon_value
                )
            result = connection.execute(
                query.order_by(
                    assessment_realized_targets.c.target_kind,
                    assessment_realized_targets.c.target_id,
                    assessment_realized_targets.c.horizon_kind,
                    assessment_realized_targets.c.horizon_value,
                    assessment_realized_targets.c.output,
                )
            )
            return [
                AssessmentRealizedTarget(
                    coordinate=_coordinate_from_mapping(row._mapping),
                    value=_optional_decimal_from_mapping(row._mapping, "value_text"),
                    status=_text_from_mapping(row._mapping, "status"),
                    provider_payload_hash=_optional_text_from_mapping(
                        row._mapping, "provider_payload_hash"
                    ),
                )
                for row in result
            ]

    def has_assessment_resolution_marker(
        self, round_id: str, schema_id: str, horizon_value: int
    ) -> bool:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(assessment_horizon_resolutions.c.round_id)
                .where(
                    assessment_horizon_resolutions.c.round_id == round_id,
                    assessment_horizon_resolutions.c.schema_id == schema_id,
                    assessment_horizon_resolutions.c.horizon_value == horizon_value,
                )
                .limit(1)
            ).first()
        return row is not None

    def all_unfinished_assessment_rounds_have_resolution_marker(
        self, schema_id: str, horizon_value: int
    ) -> bool:
        """Whether every unfinished round has completed this assessment horizon."""
        unresolved_marker = assessment_horizon_resolutions.alias("unresolved_marker")
        with self._engine.connect() as connection:
            missing_marker = connection.execute(
                select(rounds.c.round_id)
                .select_from(
                    rounds.outerjoin(
                        unresolved_marker,
                        and_(
                            unresolved_marker.c.round_id == rounds.c.round_id,
                            unresolved_marker.c.schema_id == rounds.c.schema_id,
                            unresolved_marker.c.horizon_value == horizon_value,
                        ),
                    )
                )
                .where(
                    rounds.c.schema_id == schema_id,
                    rounds.c.state != ROUND_STATE_CLOSED,
                    unresolved_marker.c.round_id.is_(None),
                )
                .limit(1)
            ).first()
        return missing_marker is None

    @staticmethod
    def _insert_assessment_output_scores(
        connection: Connection,
        round_id: str,
        schema_id: str,
        rows: Sequence[AssessmentOutputScore],
        *,
        now_iso: str,
    ) -> None:
        for row in rows:
            connection.execute(
                sqlite_insert(assessment_output_scores)
                .values(
                    round_id=round_id,
                    schema_id=schema_id,
                    miner_hotkey=row.miner_hotkey,
                    **_coordinate_values(row.coordinate),
                    error_text=None if row.error is None else str(row.error),
                    score_text=str(row.score),
                    scored_at=now_iso,
                )
                .on_conflict_do_nothing()
            )

    def record_assessment_output_scores(
        self,
        round_id: str,
        schema_id: str,
        rows: Sequence[AssessmentOutputScore],
        *,
        now_iso: str,
    ) -> None:
        """Persist per-output miner scores once by coordinate."""
        with self._engine.begin() as connection:
            self._insert_assessment_output_scores(
                connection, round_id, schema_id, rows, now_iso=now_iso
            )

    def assessment_output_scores_for_round(
        self, round_id: str, schema_id: str
    ) -> list[AssessmentOutputScore]:
        with self._engine.connect() as connection:
            result = connection.execute(
                select(
                    assessment_output_scores.c.miner_hotkey,
                    assessment_output_scores.c.target_kind,
                    assessment_output_scores.c.target_id,
                    assessment_output_scores.c.horizon_kind,
                    assessment_output_scores.c.horizon_value,
                    assessment_output_scores.c.output,
                    assessment_output_scores.c.error_text,
                    assessment_output_scores.c.score_text,
                )
                .where(
                    assessment_output_scores.c.round_id == round_id,
                    assessment_output_scores.c.schema_id == schema_id,
                )
                .order_by(
                    assessment_output_scores.c.miner_hotkey,
                    assessment_output_scores.c.target_kind,
                    assessment_output_scores.c.target_id,
                    assessment_output_scores.c.horizon_kind,
                    assessment_output_scores.c.horizon_value,
                    assessment_output_scores.c.output,
                )
            )
            return [
                AssessmentOutputScore(
                    miner_hotkey=_text_from_mapping(row._mapping, "miner_hotkey"),
                    coordinate=_coordinate_from_mapping(row._mapping),
                    score=_decimal_from_mapping(row._mapping, "score_text"),
                    error=_optional_decimal_from_mapping(row._mapping, "error_text"),
                )
                for row in result
            ]

    def assessment_output_score_count(self, round_id: str, schema_id: str) -> int:
        with self._engine.connect() as connection:
            count = connection.execute(
                select(func.count())
                .select_from(assessment_output_scores)
                .where(
                    assessment_output_scores.c.round_id == round_id,
                    assessment_output_scores.c.schema_id == schema_id,
                )
            ).scalar_one()
            return int(count)

    @staticmethod
    def _upsert_assessment_ema(
        connection: Connection,
        schema_id: str,
        row: AssessmentEmaState,
        *,
        now_iso: str,
    ) -> None:
        insert_query = sqlite_insert(assessment_miner_score_state).values(
            schema_id=schema_id,
            miner_hotkey=row.miner_hotkey,
            **_coordinate_values(row.coordinate),
            ema_text=str(row.ema),
            resolved_rounds=row.resolved_rounds,
            updated_at=now_iso,
        )
        connection.execute(
            insert_query.on_conflict_do_update(
                index_elements=(
                    "miner_hotkey",
                    "schema_id",
                    "target_kind",
                    "target_id",
                    "horizon_kind",
                    "horizon_value",
                    "output",
                ),
                set_={
                    "ema_text": insert_query.excluded.ema_text,
                    "resolved_rounds": insert_query.excluded.resolved_rounds,
                    "updated_at": insert_query.excluded.updated_at,
                },
            )
        )

    def upsert_assessment_ema(
        self,
        schema_id: str,
        row: AssessmentEmaState,
        *,
        now_iso: str,
    ) -> None:
        """Set current generic EMA state for one miner/coordinate."""
        with self._engine.begin() as connection:
            self._upsert_assessment_ema(connection, schema_id, row, now_iso=now_iso)

    def assessment_ema_states(self, schema_id: str) -> list[AssessmentEmaState]:
        with self._engine.connect() as connection:
            result = connection.execute(
                select(
                    assessment_miner_score_state.c.miner_hotkey,
                    assessment_miner_score_state.c.target_kind,
                    assessment_miner_score_state.c.target_id,
                    assessment_miner_score_state.c.horizon_kind,
                    assessment_miner_score_state.c.horizon_value,
                    assessment_miner_score_state.c.output,
                    assessment_miner_score_state.c.ema_text,
                    assessment_miner_score_state.c.resolved_rounds,
                )
                .where(assessment_miner_score_state.c.schema_id == schema_id)
                .order_by(
                    assessment_miner_score_state.c.miner_hotkey,
                    assessment_miner_score_state.c.target_kind,
                    assessment_miner_score_state.c.target_id,
                    assessment_miner_score_state.c.horizon_kind,
                    assessment_miner_score_state.c.horizon_value,
                    assessment_miner_score_state.c.output,
                )
            )
            return [
                AssessmentEmaState(
                    miner_hotkey=_text_from_mapping(row._mapping, "miner_hotkey"),
                    coordinate=_coordinate_from_mapping(row._mapping),
                    ema=_decimal_from_mapping(row._mapping, "ema_text"),
                    resolved_rounds=_int_from_mapping(row._mapping, "resolved_rounds"),
                )
                for row in result
            ]

    def has_unfinished_assessment_submission(
        self, schema_id: str, miner_hotkey: str
    ) -> bool:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(submissions.c.round_id)
                .join(
                    rounds,
                    and_(
                        rounds.c.round_id == submissions.c.round_id,
                        rounds.c.schema_id == submissions.c.schema_id,
                    ),
                )
                .where(
                    submissions.c.schema_id == schema_id,
                    submissions.c.miner_hotkey == miner_hotkey,
                    submissions.c.verdict == "accepted",
                    rounds.c.state != ROUND_STATE_CLOSED,
                )
                .limit(1)
            ).first()
        return row is not None

    def archive_assessment_ema_horizon(
        self, schema_id: str, horizon_value: int, miner_hotkeys: Sequence[str]
    ) -> bool:
        """Remove archived hotkeys and report whether their EMA state changed."""
        if not miner_hotkeys:
            return False
        with self._engine.begin() as connection:
            return self._delete_archived_assessment_ema_horizon(
                connection, schema_id, horizon_value, miner_hotkeys
            )

    @staticmethod
    def _delete_archived_assessment_ema_horizon(
        connection: Connection,
        schema_id: str,
        horizon_value: int,
        miner_hotkeys: Sequence[str],
    ) -> bool:
        result = connection.execute(
            delete(assessment_miner_score_state).where(
                assessment_miner_score_state.c.schema_id == schema_id,
                assessment_miner_score_state.c.miner_hotkey.in_(list(miner_hotkeys)),
                assessment_miner_score_state.c.horizon_value == horizon_value,
            )
        )
        return result.rowcount > 0

    @staticmethod
    def _insert_assessment_score_history(
        connection: Connection,
        round_id: str,
        schema_id: str,
        rows: Sequence[AssessmentScoreHistoryRow],
        *,
        now_iso: str,
    ) -> None:
        for row in rows:
            connection.execute(
                sqlite_insert(assessment_score_history)
                .values(
                    round_id=round_id,
                    schema_id=schema_id,
                    miner_hotkey=row.miner_hotkey,
                    **_coordinate_values(row.coordinate),
                    round_score_text=str(row.round_score),
                    ema_after_text=str(row.ema_after),
                    scored_at=now_iso,
                )
                .on_conflict_do_nothing()
            )

    def record_assessment_score_history(
        self,
        round_id: str,
        schema_id: str,
        rows: Sequence[AssessmentScoreHistoryRow],
        *,
        now_iso: str,
    ) -> None:
        """Append generic score trajectory rows once by coordinate."""
        with self._engine.begin() as connection:
            self._insert_assessment_score_history(
                connection, round_id, schema_id, rows, now_iso=now_iso
            )

    def record_assessment_scoring_pass(
        self,
        round_id: str,
        schema_id: str,
        *,
        horizon_value: int,
        realized_targets: Sequence[AssessmentRealizedTarget],
        output_scores: Sequence[AssessmentOutputScore],
        ema_updates: Sequence[AssessmentEmaState],
        score_history: Sequence[AssessmentScoreHistoryRow],
        complete: bool = True,
        now_iso: str,
        archive_hotkeys: Sequence[str] = (),
        pruned_hotkeys: Sequence[str] = (),
    ) -> None:
        """Persist one generic scoring pass atomically and idempotently.

        The horizon-resolution marker is the validator's completion signal for
        a (round, schema, horizon), independent of target/output row cardinality.
        All scoring surfaces and the marker share one transaction so a crash mid-pass rolls
        everything back and the next tick retries cleanly. Unlike the legacy
        pass, idempotency is structural, not an upstream courtesy: a replay
        of a completed round no-ops INSIDE the transaction, so the EMA can
        never double-apply and resolved_rounds can never double-count.

        Confirmed deregistrations are removed only at the pass's horizon
        coordinates, after their zero-fill. Fully decayed hotkeys are removed
        across all coordinates. Score history and output-score rows are never
        deleted.
        """
        with self._engine.begin() as connection:
            marker = connection.execute(
                select(assessment_horizon_resolutions.c.round_id)
                .where(
                    assessment_horizon_resolutions.c.round_id == round_id,
                    assessment_horizon_resolutions.c.schema_id == schema_id,
                    assessment_horizon_resolutions.c.horizon_value == horizon_value,
                )
                .limit(1)
            ).first()
            if marker is not None:
                return
            self._insert_assessment_realized_targets(
                connection, round_id, schema_id, realized_targets, now_iso=now_iso
            )
            self._insert_assessment_output_scores(
                connection, round_id, schema_id, output_scores, now_iso=now_iso
            )
            for ema_row in ema_updates:
                self._upsert_assessment_ema(
                    connection, schema_id, ema_row, now_iso=now_iso
                )
            self._insert_assessment_score_history(
                connection, round_id, schema_id, score_history, now_iso=now_iso
            )
            if archive_hotkeys:
                self._delete_archived_assessment_ema_horizon(
                    connection, schema_id, horizon_value, archive_hotkeys
                )
            if pruned_hotkeys:
                connection.execute(
                    delete(assessment_miner_score_state).where(
                        assessment_miner_score_state.c.schema_id == schema_id,
                        assessment_miner_score_state.c.miner_hotkey.in_(
                            list(pruned_hotkeys)
                        ),
                    )
                )
            if complete:
                connection.execute(
                    sqlite_insert(assessment_horizon_resolutions)
                    .values(
                        round_id=round_id,
                        schema_id=schema_id,
                        horizon_value=horizon_value,
                        resolved_at=now_iso,
                    )
                    .on_conflict_do_nothing()
                )

    def assessment_score_history_for_round(
        self, round_id: str, schema_id: str
    ) -> list[AssessmentScoreHistoryRow]:
        with self._engine.connect() as connection:
            result = connection.execute(
                select(
                    assessment_score_history.c.miner_hotkey,
                    assessment_score_history.c.target_kind,
                    assessment_score_history.c.target_id,
                    assessment_score_history.c.horizon_kind,
                    assessment_score_history.c.horizon_value,
                    assessment_score_history.c.output,
                    assessment_score_history.c.round_score_text,
                    assessment_score_history.c.ema_after_text,
                )
                .where(
                    assessment_score_history.c.round_id == round_id,
                    assessment_score_history.c.schema_id == schema_id,
                )
                .order_by(
                    assessment_score_history.c.miner_hotkey,
                    assessment_score_history.c.target_kind,
                    assessment_score_history.c.target_id,
                    assessment_score_history.c.horizon_kind,
                    assessment_score_history.c.horizon_value,
                    assessment_score_history.c.output,
                )
            )
            return [
                AssessmentScoreHistoryRow(
                    miner_hotkey=_text_from_mapping(row._mapping, "miner_hotkey"),
                    coordinate=_coordinate_from_mapping(row._mapping),
                    round_score=_decimal_from_mapping(row._mapping, "round_score_text"),
                    ema_after=_decimal_from_mapping(row._mapping, "ema_after_text"),
                )
                for row in result
            ]

    def record_weight_emission(
        self,
        *,
        schema_id: str,
        round_id: str | None,
        emitted_at_iso: str,
        block: int | None,
        min_allowed_weights: int | None,
        max_weight_limit: Decimal | None,
        metagraph_size: int,
        status: str,
        rows: Sequence[WeightEmissionRow],
        submission_block: int | None = None,
        confirmation_state: str | None = None,
        confirmation_deadline_block: int | None = None,
        cr4_reveal_deadline_block: int | None = None,
        baseline_last_update_block: int | None = None,
        period_blocks: int | None = None,
        confirmed_at_iso: str | None = None,
        chain_identity: str | None = None,
        netuid: int | None = None,
        validator_uid: int | None = None,
        validator_hotkey: str | None = None,
        submission_mode: str | None = None,
        intent_hash: str | None = None,
        protocol_version_key: int | None = None,
        commitment_hash: str | None = None,
        reveal_round: int | None = None,
        commitment_observed_block: int | None = None,
    ) -> int:
        if cr4_reveal_deadline_block is not None and submission_mode != "cr4":
            raise ValueError("CR4 reveal deadline requires CR4 submission mode")
        _validate_weight_emission_transport(
            status, confirmation_state, confirmed_at_iso
        )
        _validate_weight_emission_numbers(
            submission_block,
            baseline_last_update_block,
            period_blocks,
            confirmation_deadline_block,
            cr4_reveal_deadline_block,
            netuid,
            validator_uid,
            reveal_round,
            commitment_observed_block,
        )
        _validate_weight_emission_evidence(
            confirmation_state,
            submission_block,
            baseline_last_update_block,
            period_blocks,
            confirmation_deadline_block,
            chain_identity,
            netuid,
            validator_uid,
            validator_hotkey,
            submission_mode,
            intent_hash,
            protocol_version_key,
            commitment_hash,
            reveal_round,
            rows,
        )
        with self._engine.begin() as connection:
            inserted = connection.execute(
                insert(weight_emission_batches).values(
                    schema_id=schema_id,
                    round_id=round_id,
                    emitted_at=emitted_at_iso,
                    block=block,
                    min_allowed_weights=min_allowed_weights,
                    max_weight_limit_text=(
                        None if max_weight_limit is None else str(max_weight_limit)
                    ),
                    metagraph_size=metagraph_size,
                    status=status,
                    submission_block=submission_block,
                    confirmation_state=confirmation_state,
                    confirmed_at=confirmed_at_iso,
                    confirmation_deadline_block=confirmation_deadline_block,
                    cr4_reveal_deadline_block=cr4_reveal_deadline_block,
                    baseline_last_update_block=baseline_last_update_block,
                    period_blocks=period_blocks,
                    chain_identity=chain_identity,
                    netuid=netuid,
                    validator_uid=validator_uid,
                    validator_hotkey=validator_hotkey,
                    submission_mode=submission_mode,
                    intent_hash=intent_hash,
                    commitment_hash=commitment_hash,
                    reveal_round=reveal_round,
                    commitment_observed_block=commitment_observed_block,
                )
            ).inserted_primary_key
            if inserted is None:
                raise RuntimeError("weight emission batch insert returned no key")
            batch_id = int(inserted[0])
            if rows:
                connection.execute(
                    insert(weight_emission_rows),
                    [
                        {
                            "batch_id": batch_id,
                            "miner_hotkey": row.miner_hotkey,
                            "uid": row.uid,
                            "blended_score_text": (
                                None
                                if row.blended_score is None
                                else str(row.blended_score)
                            ),
                            "weight_norm_precap_text": (
                                None
                                if row.weight_norm_precap is None
                                else str(row.weight_norm_precap)
                            ),
                            "weight_processed_text": str(row.weight_processed),
                            "weight_u16": row.weight_u16,
                            "emitted": False,
                        }
                        for row in rows
                    ],
                )
            return int(batch_id)

    def transition_weight_emission_attempt(
        self,
        *,
        batch_id: int,
        status: str,
        confirmation_state: str,
        submission_mode: str,
        commitment_hash: str | None,
        reveal_round: int | None,
        confirmation_deadline_block: int | None,
        cr4_reveal_deadline_block: int | None = None,
    ) -> None:
        """Transition one durable prepared attempt after its sole SDK call."""
        if status not in WEIGHT_EMISSION_STATUSES:
            raise ValueError(f"unknown weight emission status {status!r}")
        if confirmation_state not in {"submitted", "ambiguous", "failed"}:
            raise ValueError(
                f"invalid prepared emission transition {confirmation_state!r}"
            )
        if (confirmation_state == "submitted" and status != "submitted") or (
            confirmation_state == "ambiguous" and status != "error"
        ):
            raise ValueError(
                f"confirmation state {confirmation_state!r} does not match "
                f"transport status {status!r}"
            )
        if confirmation_state == "failed" and status not in {"failed", "error"}:
            raise ValueError(
                f"confirmation state 'failed' does not match transport status {status!r}"
            )
        if submission_mode not in {"direct", "cr4"}:
            raise ValueError(f"unknown weight submission mode {submission_mode!r}")
        if (commitment_hash is None) != (reveal_round is None):
            raise ValueError(
                "CR4 commitment hash and reveal round must be set together"
            )
        if commitment_hash is not None and submission_mode != "cr4":
            raise ValueError("CR4 commitment metadata requires CR4 submission mode")
        if cr4_reveal_deadline_block is not None and submission_mode != "cr4":
            raise ValueError("CR4 reveal deadline requires CR4 submission mode")
        with self._engine.begin() as connection:
            result = connection.execute(
                update(weight_emission_batches)
                .where(
                    weight_emission_batches.c.id == batch_id,
                    weight_emission_batches.c.confirmation_state == "prepared",
                )
                .values(
                    status=status,
                    confirmation_state=confirmation_state,
                    submission_mode=submission_mode,
                    commitment_hash=commitment_hash,
                    reveal_round=reveal_round,
                    confirmation_deadline_block=(
                        weight_emission_batches.c.confirmation_deadline_block
                        if confirmation_deadline_block is None
                        else func.max(
                            weight_emission_batches.c.confirmation_deadline_block,
                            confirmation_deadline_block,
                        )
                    ),
                    cr4_reveal_deadline_block=(
                        weight_emission_batches.c.cr4_reveal_deadline_block
                        if cr4_reveal_deadline_block is None
                        else func.max(
                            func.coalesce(
                                weight_emission_batches.c.cr4_reveal_deadline_block,
                                cr4_reveal_deadline_block,
                            ),
                            cr4_reveal_deadline_block,
                        )
                    ),
                )
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    f"prepared weight emission {batch_id} was not transitionable"
                )

    def resolve_weight_emission_confirmations(
        self,
        *,
        schema_id: str,
        snapshot: WeightEmissionChainSnapshot,
        finality_margin_blocks: int,
        confirmed_at_iso: str,
    ) -> WeightEmissionConfirmationResolution:
        """Resolve every open emission using one successful metagraph snapshot."""
        if finality_margin_blocks < 0:
            raise ValueError("finality_margin_blocks must be non-negative")
        _validate_weight_chain_snapshot(snapshot)
        confirmed = 0
        unconfirmed = 0
        with self._engine.begin() as connection:
            open_batches = list(
                connection.execute(
                    select(
                        weight_emission_batches.c.id,
                        weight_emission_batches.c.submission_block,
                        weight_emission_batches.c.baseline_last_update_block,
                        weight_emission_batches.c.period_blocks,
                        weight_emission_batches.c.confirmation_deadline_block,
                        weight_emission_batches.c.cr4_reveal_deadline_block,
                        weight_emission_batches.c.confirmation_state,
                        weight_emission_batches.c.chain_identity,
                        weight_emission_batches.c.netuid,
                        weight_emission_batches.c.validator_uid,
                        weight_emission_batches.c.validator_hotkey,
                        weight_emission_batches.c.submission_mode,
                        weight_emission_batches.c.intent_hash,
                        weight_emission_batches.c.commitment_hash,
                        weight_emission_batches.c.reveal_round,
                        weight_emission_batches.c.commitment_observed_block,
                        weight_emission_batches.c.commitment_observed_last_update_block,
                    ).where(
                        weight_emission_batches.c.schema_id == schema_id,
                        weight_emission_batches.c.confirmation_state.in_(
                            OPEN_WEIGHT_EMISSION_CONFIRMATION_STATES
                        ),
                    )
                )
            )
            for batch in open_batches:
                batch_id = int(batch._mapping["id"])
                submission_block = batch._mapping["submission_block"]
                baseline_block = batch._mapping["baseline_last_update_block"]
                deadline = batch._mapping["confirmation_deadline_block"]
                reveal_deadline = batch._mapping["cr4_reveal_deadline_block"]
                confirmation_state = str(batch._mapping["confirmation_state"])
                if (
                    batch._mapping["chain_identity"] != snapshot.chain_identity
                    or batch._mapping["netuid"] != snapshot.netuid
                    or batch._mapping["validator_uid"] != snapshot.validator_uid
                    or batch._mapping["validator_hotkey"] != snapshot.validator_hotkey
                ):
                    continue
                if not isinstance(submission_block, int) or not isinstance(
                    baseline_block, int
                ):
                    continue
                if snapshot.block < max(submission_block, baseline_block):
                    continue
                expected_targets = _expected_weight_targets(connection, batch_id)
                expected_vector = tuple(
                    (uid, weight) for uid, _hotkey, weight in expected_targets
                )
                snapshot_hotkeys = dict(snapshot.hotkeys)
                vector_matches = expected_vector == tuple(
                    sorted(snapshot.weights)
                ) and all(
                    snapshot_hotkeys.get(uid) == hotkey
                    for uid, hotkey, _weight in expected_targets
                )
                stored_intent_hash = str(batch._mapping["intent_hash"])
                try:
                    recorded_protocol_version = _recorded_protocol_version(
                        stored_intent_hash
                    )
                except ValueError:
                    intent_matches = False
                else:
                    intent_matches = (
                        recorded_protocol_version is None
                        or recorded_protocol_version == CURRENT_VERSION_KEY
                    ) and stored_intent_hash == _weight_intent_hash(
                        chain_identity=snapshot.chain_identity,
                        netuid=snapshot.netuid,
                        validator_uid=snapshot.validator_uid,
                        validator_hotkey=snapshot.validator_hotkey,
                        targets=expected_targets,
                        protocol_version_key=recorded_protocol_version,
                    )
                submission_mode = batch._mapping["submission_mode"]
                decision = (
                    _direct_weight_resolution(
                        snapshot=snapshot,
                        submission_block=submission_block,
                        baseline_block=baseline_block,
                        deadline=(deadline if isinstance(deadline, int) else None),
                        finality_margin_blocks=finality_margin_blocks,
                        vector_matches=vector_matches and intent_matches,
                    )
                    if submission_mode == "direct"
                    else _cr4_weight_resolution(
                        snapshot=snapshot,
                        submission_block=submission_block,
                        baseline_block=baseline_block,
                        period_blocks=int(batch._mapping["period_blocks"]),
                        deadline=(deadline if isinstance(deadline, int) else None),
                        reveal_deadline=(
                            reveal_deadline
                            if isinstance(reveal_deadline, int)
                            else None
                        ),
                        finality_margin_blocks=finality_margin_blocks,
                        commitment_hash=batch._mapping["commitment_hash"],
                        reveal_round=batch._mapping["reveal_round"],
                        observed_block=batch._mapping["commitment_observed_block"],
                        observed_last_update_block=batch._mapping[
                            "commitment_observed_last_update_block"
                        ],
                        vector_matches=vector_matches and intent_matches,
                        reveal_scan_complete=snapshot.reveal_scan_complete,
                    )
                )
                newly_confirmed, newly_unconfirmed = _apply_weight_resolution(
                    connection,
                    batch_id=batch_id,
                    confirmation_state=confirmation_state,
                    decision=decision,
                    confirmed_at_iso=confirmed_at_iso,
                )
                confirmed += newly_confirmed
                unconfirmed += newly_unconfirmed
        return WeightEmissionConfirmationResolution(
            confirmed=confirmed, unconfirmed=unconfirmed
        )

    def has_open_weight_emission_confirmation(self, *, schema_id: str) -> bool:
        """Whether a submitted batch still needs an inclusion decision."""
        with self._engine.connect() as connection:
            open_submissions = connection.execute(
                select(func.count())
                .select_from(weight_emission_batches)
                .where(
                    weight_emission_batches.c.schema_id == schema_id,
                    weight_emission_batches.c.confirmation_state.in_(
                        OPEN_WEIGHT_EMISSION_CONFIRMATION_STATES
                    ),
                )
            ).scalar_one()
        return int(open_submissions) > 0

    def has_confirmed_weight_emission(self, *, schema_id: str) -> bool:
        """Whether any emission batch for the schema reached a confirmed state.

        A direct existence check, uncapped, so a caller asking "does this
        database contain a confirmed batch?" is not misled by a window of only
        the most recent batches when many later attempts followed one.
        """
        with self._engine.connect() as connection:
            confirmed = connection.execute(
                select(func.count())
                .select_from(weight_emission_batches)
                .where(
                    weight_emission_batches.c.schema_id == schema_id,
                    weight_emission_batches.c.confirmation_state == "confirmed",
                )
            ).scalar_one()
        return int(confirmed) > 0

    def weight_emission_startup_fence(
        self, *, schema_id: str, protocol_version_key: int
    ) -> int | None:
        """Return the durable startup fence for a protocol lease."""
        with self._engine.connect() as connection:
            value = connection.execute(
                select(weight_emission_startup_fences.c.fence_block).where(
                    weight_emission_startup_fences.c.schema_id == schema_id,
                    weight_emission_startup_fences.c.protocol_version_key
                    == protocol_version_key,
                )
            ).scalar_one_or_none()
        return int(value) if isinstance(value, int) else None

    def record_weight_emission_startup_fence(
        self, *, schema_id: str, protocol_version_key: int, fence_block: int
    ) -> None:
        """Persist one startup fence per schema and protocol lease."""
        with self._engine.begin() as connection:
            connection.execute(
                sqlite_insert(weight_emission_startup_fences)
                .values(
                    schema_id=schema_id,
                    protocol_version_key=protocol_version_key,
                    fence_block=fence_block,
                )
                .on_conflict_do_nothing(
                    index_elements=["schema_id", "protocol_version_key"]
                )
            )

    def cr4_reveal_scan_window(
        self, *, schema_id: str, finalized_block: int
    ) -> Cr4RevealScanWindow | None:
        """Return one bounded, unscanned CR4 reveal-evidence window."""
        with self._engine.connect() as connection:
            row = connection.execute(
                select(
                    weight_emission_batches.c.id,
                    weight_emission_batches.c.submission_block,
                    weight_emission_batches.c.cr4_reveal_deadline_block,
                    weight_emission_batches.c.reveal_scan_cursor_block,
                ).where(
                    weight_emission_batches.c.schema_id == schema_id,
                    weight_emission_batches.c.submission_mode == "cr4",
                    weight_emission_batches.c.confirmation_state.in_(
                        OPEN_WEIGHT_EMISSION_CONFIRMATION_STATES
                    ),
                    weight_emission_batches.c.cr4_reveal_deadline_block.is_not(None),
                )
            ).one_or_none()
        if row is None:
            return None
        submission_block = int(row._mapping["submission_block"])
        reveal_deadline = int(row._mapping["cr4_reveal_deadline_block"])
        cursor = row._mapping["reveal_scan_cursor_block"]
        start_block = max(
            submission_block,
            int(cursor) + 1 if isinstance(cursor, int) else submission_block,
        )
        end_block = min(
            finalized_block,
            reveal_deadline,
            start_block + CR4_REVEAL_SCAN_BATCH_BLOCKS - 1,
        )
        if start_block > end_block:
            return None
        return Cr4RevealScanWindow(
            batch_id=int(row._mapping["id"]),
            start_block=start_block,
            end_block=end_block,
            complete=end_block >= reveal_deadline,
        )

    def advance_cr4_reveal_scan_cursor(
        self, *, batch_id: int, scanned_through: int
    ) -> None:
        """Advance a CR4 evidence cursor after a scan batch succeeds."""
        with self._engine.begin() as connection:
            connection.execute(
                update(weight_emission_batches)
                .where(
                    weight_emission_batches.c.id == batch_id,
                    weight_emission_batches.c.confirmation_state.in_(
                        OPEN_WEIGHT_EMISSION_CONFIRMATION_STATES
                    ),
                )
                .values(
                    reveal_scan_cursor_block=func.max(
                        func.coalesce(
                            weight_emission_batches.c.reveal_scan_cursor_block,
                            scanned_through,
                        ),
                        scanned_through,
                    )
                )
            )

    def weight_emission_confirmation_health(
        self, *, schema_id: str, current_block: int | None
    ) -> WeightEmissionConfirmationHealth:
        """Return confirmation counters without exposing provider error details."""
        with self._engine.connect() as connection:
            last_confirmed_at = connection.execute(
                select(func.max(weight_emission_batches.c.confirmed_at)).where(
                    weight_emission_batches.c.schema_id == schema_id,
                    weight_emission_batches.c.confirmation_state == "confirmed",
                )
            ).scalar_one()
            oldest_open_block = connection.execute(
                select(func.min(weight_emission_batches.c.submission_block)).where(
                    weight_emission_batches.c.schema_id == schema_id,
                    weight_emission_batches.c.confirmation_state.in_(
                        OPEN_WEIGHT_EMISSION_CONFIRMATION_STATES
                    ),
                )
            ).scalar_one()
            oldest_open_deadline = connection.execute(
                select(
                    func.min(
                        func.coalesce(
                            weight_emission_batches.c.cr4_reveal_deadline_block,
                            weight_emission_batches.c.confirmation_deadline_block,
                        )
                    )
                ).where(
                    weight_emission_batches.c.schema_id == schema_id,
                    weight_emission_batches.c.confirmation_state.in_(
                        OPEN_WEIGHT_EMISSION_CONFIRMATION_STATES
                    ),
                )
            ).scalar_one()
            latest_unconfirmed_block = connection.execute(
                select(func.max(weight_emission_batches.c.submission_block)).where(
                    weight_emission_batches.c.schema_id == schema_id,
                    weight_emission_batches.c.confirmation_state == "unconfirmed",
                )
            ).scalar_one()
            latest_confirmed_block = connection.execute(
                select(func.max(weight_emission_batches.c.submission_block)).where(
                    weight_emission_batches.c.schema_id == schema_id,
                    weight_emission_batches.c.confirmation_state == "confirmed",
                )
            ).scalar_one()
            open_submissions = connection.execute(
                select(func.count())
                .select_from(weight_emission_batches)
                .where(
                    weight_emission_batches.c.schema_id == schema_id,
                    weight_emission_batches.c.confirmation_state.in_(
                        OPEN_WEIGHT_EMISSION_CONFIRMATION_STATES
                    ),
                )
            ).scalar_one()
            failed_submissions = connection.execute(
                select(func.count())
                .select_from(weight_emission_batches)
                .where(
                    weight_emission_batches.c.schema_id == schema_id,
                    weight_emission_batches.c.confirmation_state == "failed",
                )
            ).scalar_one()
        return WeightEmissionConfirmationHealth(
            last_confirmed_at=(
                last_confirmed_at if isinstance(last_confirmed_at, str) else None
            ),
            open_submissions=int(open_submissions),
            oldest_open_age_blocks=(
                None
                if current_block is None or not isinstance(oldest_open_block, int)
                else max(0, current_block - oldest_open_block)
            ),
            oldest_open_deadline_block=(
                oldest_open_deadline if isinstance(oldest_open_deadline, int) else None
            ),
            latest_unconfirmed_submission_block=(
                latest_unconfirmed_block
                if isinstance(latest_unconfirmed_block, int)
                else None
            ),
            latest_confirmed_submission_block=(
                latest_confirmed_block
                if isinstance(latest_confirmed_block, int)
                else None
            ),
            failed_submissions_total=int(failed_submissions),
        )

    def weight_emission_history(
        self, schema_id: str, *, limit: int = 1000
    ) -> list[dict[str, object]]:
        """Return redacted weight-emission batches and rows for evidence tools."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._engine.connect() as connection:
            batch_rows = list(
                connection.execute(
                    select(weight_emission_batches)
                    .where(weight_emission_batches.c.schema_id == schema_id)
                    .order_by(weight_emission_batches.c.id.desc())
                    .limit(limit)
                )
            )
            rows_by_batch: dict[int, list[dict[str, object]]] = {
                int(batch._mapping["id"]): [] for batch in batch_rows
            }
            confirmed_by_batch = {
                int(batch._mapping["id"]): (
                    batch._mapping["confirmation_state"] == "confirmed"
                )
                for batch in batch_rows
            }
            if rows_by_batch:
                row_result = connection.execute(
                    select(weight_emission_rows)
                    .where(weight_emission_rows.c.batch_id.in_(rows_by_batch.keys()))
                    .order_by(
                        weight_emission_rows.c.batch_id,
                        weight_emission_rows.c.uid,
                    )
                )
                for row in row_result:
                    batch_confirmed = confirmed_by_batch[int(row._mapping["batch_id"])]
                    row_confirmed = (
                        batch_confirmed and row._mapping["weight_u16"] is not None
                    )
                    rows_by_batch[int(row._mapping["batch_id"])].append(
                        {
                            "miner_hotkey": row._mapping["miner_hotkey"],
                            "uid": int(row._mapping["uid"]),
                            "blended_score_text": row._mapping["blended_score_text"],
                            "weight_norm_precap_text": row._mapping[
                                "weight_norm_precap_text"
                            ],
                            "weight_processed_text": row._mapping[
                                "weight_processed_text"
                            ],
                            "weight_u16": (
                                None
                                if row._mapping["weight_u16"] is None
                                else int(row._mapping["weight_u16"])
                            ),
                            "emitted": row_confirmed,
                            "confirmed": row_confirmed,
                        }
                    )
            batches: list[dict[str, object]] = []
            for batch in batch_rows:
                batch_id = int(batch._mapping["id"])
                batches.append(
                    {
                        "id": batch_id,
                        "schema_id": batch._mapping["schema_id"],
                        "round_id": batch._mapping["round_id"],
                        "attempted_at": batch._mapping["emitted_at"],
                        "block": batch._mapping["block"],
                        "min_allowed_weights": batch._mapping["min_allowed_weights"],
                        "max_weight_limit_text": batch._mapping[
                            "max_weight_limit_text"
                        ],
                        "metagraph_size": batch._mapping["metagraph_size"],
                        "status": batch._mapping["status"],
                        "submission_block": batch._mapping["submission_block"],
                        "confirmation_state": batch._mapping["confirmation_state"],
                        "confirmed_at": batch._mapping["confirmed_at"],
                        "confirmation_deadline_block": batch._mapping[
                            "confirmation_deadline_block"
                        ],
                        "cr4_reveal_deadline_block": batch._mapping[
                            "cr4_reveal_deadline_block"
                        ],
                        "baseline_last_update_block": batch._mapping[
                            "baseline_last_update_block"
                        ],
                        "period_blocks": batch._mapping["period_blocks"],
                        "chain_identity": batch._mapping["chain_identity"],
                        "netuid": batch._mapping["netuid"],
                        "validator_uid": batch._mapping["validator_uid"],
                        "validator_hotkey": batch._mapping["validator_hotkey"],
                        "submission_mode": batch._mapping["submission_mode"],
                        "intent_hash": batch._mapping["intent_hash"],
                        "commitment_hash": batch._mapping["commitment_hash"],
                        "reveal_round": batch._mapping["reveal_round"],
                        "commitment_observed_block": batch._mapping[
                            "commitment_observed_block"
                        ],
                        "commitment_observed_last_update_block": batch._mapping[
                            "commitment_observed_last_update_block"
                        ],
                        "rows": rows_by_batch[batch_id],
                    }
                )
            return batches
