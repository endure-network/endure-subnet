"""Storage tables (spec §5; Forge lending activation spec §Batch 2A).

Conventions: Decimal values are stored as TEXT (``*_text`` columns) per the
repo Decimal policy; bps values are integers; timestamps are ISO-8601 UTC
strings. The scoring pipeline reads and writes only these tables, which is
what makes scoring a pure, replayable function of the database.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    Table,
    Text,
    UniqueConstraint,
    text,
)

from endure.storage.migrations.metadata import metadata

_COORDINATE_NAMES = (
    "target_kind",
    "target_id",
    "horizon_kind",
    "horizon_value",
    "output",
)


def _coordinate_columns() -> tuple[Column[str] | Column[int], ...]:
    return (
        Column("target_kind", Text, nullable=False),
        Column("target_id", Text, nullable=False),
        Column("horizon_kind", Text, nullable=False),
        Column("horizon_value", Integer, nullable=False),
        Column("output", Text, nullable=False),
    )


def _coordinate_unique(*prefix_columns: str) -> UniqueConstraint:
    return UniqueConstraint(*prefix_columns, *_COORDINATE_NAMES)


rounds = Table(
    "rounds",
    metadata,
    Column("round_id", Text, primary_key=True),
    Column("schema_id", Text, primary_key=True),
    Column("state", Text, nullable=False),
    Column("universe_stale", Boolean, nullable=False, server_default=text("0")),
    Column("degraded", Boolean, nullable=False, server_default=text("0")),
    Column("commit_open_at", Text, nullable=False),
    Column("commit_close_at", Text, nullable=False),
    Column("reveal_open_at", Text, nullable=False),
    Column("reveal_close_at", Text, nullable=False),
    Column("publication_available_at", Text, nullable=False),
    Column("t0_close_at", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
)

# No FK to rounds(round_id, schema_id) by design: open_round inserts the
# universe snapshot BEFORE the round row (so a round never opens with a missing
# universe), which an FK — enforced via PRAGMA foreign_keys=ON — would reject.
universe_snapshots = Table(
    "universe_snapshots",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("round_id", Text, nullable=False),
    Column("schema_id", Text, nullable=False),
    Column("tickers_json", Text, nullable=False),
    Column("source_hash", Text, nullable=False),
    Column("fetched_at", Text, nullable=False),
    UniqueConstraint("round_id", "schema_id"),
)

submissions = Table(
    "submissions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("round_id", Text, nullable=False),
    Column("schema_id", Text, nullable=False),
    Column("miner_hotkey", Text, nullable=False),
    Column("commit_hash", Text, nullable=True),
    Column("commit_count", Integer, nullable=False, server_default=text("0")),
    Column("reveal_count", Integer, nullable=False, server_default=text("0")),
    Column("nonce_hex", Text, nullable=True),
    Column("committed_at", Text, nullable=True),
    Column("revealed_at", Text, nullable=True),
    Column("bundle_json", Text, nullable=True),
    Column("verdict", Text, nullable=False),
    Column("rejection_code", Text, nullable=True),
    UniqueConstraint("round_id", "schema_id", "miner_hotkey"),
    ForeignKeyConstraint(
        ["round_id", "schema_id"], ["rounds.round_id", "rounds.schema_id"]
    ),
)

consensus_bundle_snapshots = Table(
    "consensus_bundle_snapshots",
    metadata,
    Column("round_id", Text, primary_key=True),
    Column("schema_id", Text, primary_key=True),
    Column("miner_hotkey", Text, primary_key=True),
    Column("bundle_json", Text, nullable=False),
    Column("snapshotted_at", Text, nullable=False),
    ForeignKeyConstraint(
        ["round_id", "schema_id"], ["rounds.round_id", "rounds.schema_id"]
    ),
)

assessment_consensus = Table(
    "assessment_consensus",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("round_id", Text, nullable=False),
    Column("schema_id", Text, nullable=False),
    *_coordinate_columns(),
    Column("value_text", Text, nullable=False),
    Column("dispersion_text", Text, nullable=False),
    Column("n_submitters", Integer, nullable=False),
    Column("computed_at", Text, nullable=False),
    _coordinate_unique("round_id", "schema_id"),
    ForeignKeyConstraint(
        ["round_id", "schema_id"], ["rounds.round_id", "rounds.schema_id"]
    ),
)

assessment_realized_targets = Table(
    "assessment_realized_targets",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("round_id", Text, nullable=False),
    Column("schema_id", Text, nullable=False),
    *_coordinate_columns(),
    Column("value_text", Text, nullable=True),
    Column("status", Text, nullable=False),
    Column("provider_payload_hash", Text, nullable=True),
    Column("fetched_at", Text, nullable=False),
    _coordinate_unique("round_id", "schema_id"),
    ForeignKeyConstraint(
        ["round_id", "schema_id"], ["rounds.round_id", "rounds.schema_id"]
    ),
)

assessment_horizon_resolutions = Table(
    "assessment_horizon_resolutions",
    metadata,
    Column("round_id", Text, primary_key=True),
    Column("schema_id", Text, primary_key=True),
    Column("horizon_value", Integer, primary_key=True),
    Column("resolved_at", Text, nullable=False),
    ForeignKeyConstraint(
        ["round_id", "schema_id"], ["rounds.round_id", "rounds.schema_id"]
    ),
)

assessment_output_scores = Table(
    "assessment_output_scores",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("round_id", Text, nullable=False),
    Column("schema_id", Text, nullable=False),
    Column("miner_hotkey", Text, nullable=False),
    *_coordinate_columns(),
    Column("error_text", Text, nullable=True),
    Column("score_text", Text, nullable=False),
    Column("scored_at", Text, nullable=False),
    _coordinate_unique("round_id", "schema_id", "miner_hotkey"),
    ForeignKeyConstraint(
        ["round_id", "schema_id"], ["rounds.round_id", "rounds.schema_id"]
    ),
)

assessment_miner_score_state = Table(
    "assessment_miner_score_state",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("miner_hotkey", Text, nullable=False),
    Column("schema_id", Text, nullable=False),
    *_coordinate_columns(),
    Column("ema_text", Text, nullable=False),
    Column("resolved_rounds", Integer, nullable=False, server_default=text("0")),
    Column("updated_at", Text, nullable=False),
    _coordinate_unique("miner_hotkey", "schema_id"),
)

assessment_score_history = Table(
    "assessment_score_history",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("round_id", Text, nullable=False),
    Column("schema_id", Text, nullable=False),
    Column("miner_hotkey", Text, nullable=False),
    *_coordinate_columns(),
    Column("round_score_text", Text, nullable=False),
    Column("ema_after_text", Text, nullable=False),
    Column("scored_at", Text, nullable=False),
    _coordinate_unique("round_id", "schema_id", "miner_hotkey"),
    ForeignKeyConstraint(
        ["round_id", "schema_id"], ["rounds.round_id", "rounds.schema_id"]
    ),
)

weight_emission_startup_fences = Table(
    "weight_emission_startup_fences",
    metadata,
    Column("schema_id", Text, primary_key=True),
    Column("protocol_version_key", Integer, primary_key=True),
    Column("fence_block", Integer, nullable=False),
    CheckConstraint("protocol_version_key >= 0", name="ck_startup_fence_protocol_key"),
    CheckConstraint("fence_block >= 0", name="ck_startup_fence_block"),
)


weight_emission_batches = Table(
    "weight_emission_batches",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("schema_id", Text, nullable=False),
    Column("round_id", Text, nullable=True),
    Column("emitted_at", Text, nullable=False),
    Column("block", Integer, nullable=True),
    Column("min_allowed_weights", Integer, nullable=True),
    Column("max_weight_limit_text", Text, nullable=True),
    Column("metagraph_size", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("submission_block", Integer, nullable=True),
    Column("confirmation_state", Text, nullable=True),
    Column("confirmed_at", Text, nullable=True),
    Column("confirmation_deadline_block", Integer, nullable=True),
    Column("cr4_reveal_deadline_block", Integer, nullable=True),
    Column("reveal_scan_cursor_block", Integer, nullable=True),
    Column("baseline_last_update_block", Integer, nullable=True),
    Column("period_blocks", Integer, nullable=True),
    Column("chain_identity", Text, nullable=True),
    Column("netuid", Integer, nullable=True),
    Column("validator_uid", Integer, nullable=True),
    Column("validator_hotkey", Text, nullable=True),
    Column("submission_mode", Text, nullable=True),
    Column("intent_hash", Text, nullable=True),
    Column("commitment_hash", Text, nullable=True),
    Column("reveal_round", Integer, nullable=True),
    Column("commitment_observed_block", Integer, nullable=True),
    Column("commitment_observed_last_update_block", Integer, nullable=True),
    CheckConstraint(
        "confirmation_state IS NULL OR confirmation_state IN "
        "('prepared', 'submitted', 'ambiguous', 'confirmed', 'unconfirmed', 'failed')",
        name="ck_weight_emission_confirmation_state",
    ),
    CheckConstraint(
        "submission_block IS NULL OR submission_block >= 0",
        name="ck_weight_emission_submission_block",
    ),
    CheckConstraint(
        "baseline_last_update_block IS NULL OR baseline_last_update_block >= 0",
        name="ck_weight_emission_baseline_block",
    ),
    CheckConstraint(
        "period_blocks IS NULL OR period_blocks > 0",
        name="ck_weight_emission_period_blocks",
    ),
    CheckConstraint(
        "confirmation_deadline_block IS NULL OR confirmation_deadline_block >= 0",
        name="ck_weight_emission_deadline_block",
    ),
    CheckConstraint(
        "netuid IS NULL OR netuid >= 0",
        name="ck_weight_emission_netuid",
    ),
    CheckConstraint(
        "validator_uid IS NULL OR validator_uid >= 0",
        name="ck_weight_emission_validator_uid",
    ),
    CheckConstraint(
        "submission_mode IS NULL OR submission_mode IN ('direct', 'cr4')",
        name="ck_weight_emission_submission_mode",
    ),
    CheckConstraint(
        "reveal_round IS NULL OR reveal_round >= 0",
        name="ck_weight_emission_reveal_round",
    ),
    CheckConstraint(
        "commitment_observed_block IS NULL OR commitment_observed_block >= 0",
        name="ck_weight_emission_commitment_observed_block",
    ),
    CheckConstraint(
        "commitment_observed_last_update_block IS NULL OR "
        "commitment_observed_last_update_block >= 0",
        name="ck_weight_emission_commitment_observed_last_update_block",
    ),
    CheckConstraint(
        "confirmation_state NOT IN ('prepared', 'submitted', 'ambiguous') OR "
        "(submission_block IS NOT NULL AND baseline_last_update_block IS NOT NULL "
        "AND period_blocks IS NOT NULL AND confirmation_deadline_block IS NOT NULL "
        "AND chain_identity IS NOT NULL "
        "AND netuid IS NOT NULL AND validator_uid IS NOT NULL "
        "AND validator_hotkey IS NOT NULL AND submission_mode IS NOT NULL "
        "AND intent_hash IS NOT NULL)",
        name="ck_weight_emission_open_fields",
    ),
    CheckConstraint(
        "confirmation_state NOT IN ('confirmed', 'unconfirmed') OR "
        "(submission_block IS NOT NULL AND baseline_last_update_block IS NOT NULL "
        "AND period_blocks IS NOT NULL AND chain_identity IS NOT NULL "
        "AND netuid IS NOT NULL AND validator_uid IS NOT NULL "
        "AND validator_hotkey IS NOT NULL AND submission_mode IS NOT NULL "
        "AND intent_hash IS NOT NULL)",
        name="ck_weight_emission_resolution_fields",
    ),
    CheckConstraint(
        "(commitment_hash IS NULL AND reveal_round IS NULL) OR "
        "(submission_mode = 'cr4' AND commitment_hash IS NOT NULL "
        "AND reveal_round IS NOT NULL)",
        name="ck_weight_emission_cr4_metadata",
    ),
    CheckConstraint(
        "confirmation_deadline_block IS NULL OR submission_block IS NULL OR "
        "period_blocks IS NULL OR confirmation_deadline_block >= "
        "submission_block + period_blocks AND confirmation_deadline_block <= "
        "submission_block + (2 * period_blocks)",
        name="ck_weight_emission_safe_deadline",
    ),
    CheckConstraint(
        "submission_block IS NULL OR baseline_last_update_block IS NULL OR "
        "submission_block >= baseline_last_update_block",
        name="ck_weight_emission_submission_after_baseline",
    ),
    CheckConstraint(
        "(confirmation_state NOT IN ('prepared', 'ambiguous') OR status = 'error') "
        "AND (confirmation_state != 'submitted' OR status = 'submitted') "
        "AND (confirmation_state != 'failed' OR status IN ('failed', 'error'))",
        name="ck_weight_emission_status_state",
    ),
    CheckConstraint(
        "CASE WHEN confirmation_state = 'confirmed' THEN confirmed_at IS NOT NULL "
        "ELSE confirmed_at IS NULL END",
        name="ck_weight_emission_confirmed_at",
    ),
)

weight_emission_rows = Table(
    "weight_emission_rows",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("batch_id", Integer, nullable=False),
    Column("miner_hotkey", Text, nullable=False),
    Column("uid", Integer, nullable=False),
    Column("blended_score_text", Text, nullable=True),
    Column("weight_norm_precap_text", Text, nullable=True),
    Column("weight_processed_text", Text, nullable=False),
    Column("weight_u16", Integer, nullable=True),
    Column("emitted", Boolean, nullable=False, server_default=text("0")),
    CheckConstraint(
        "emitted = 0 OR weight_u16 IS NOT NULL",
        name="ck_weight_emission_row_emitted_weight",
    ),
    CheckConstraint(
        "emitted IN (0, 1)",
        name="ck_weight_emission_row_emitted_boolean",
    ),
    ForeignKeyConstraint(["batch_id"], ["weight_emission_batches.id"]),
    UniqueConstraint("batch_id", "uid"),
)


# Hot-query indexes (migration 0004). Uniques above already
# index their leading columns; these cover the remaining access paths.
Index("ix_rounds_schema_state", rounds.c.schema_id, rounds.c.state)
Index(
    "ix_submissions_round_verdict",
    submissions.c.round_id,
    submissions.c.schema_id,
    submissions.c.verdict,
)
Index(
    "ix_weight_emission_batches_schema_confirmation",
    weight_emission_batches.c.schema_id,
    weight_emission_batches.c.confirmation_state,
)
Index(
    "uq_weight_emission_open_schema",
    weight_emission_batches.c.schema_id,
    unique=True,
    sqlite_where=text("confirmation_state IN ('prepared', 'submitted', 'ambiguous')"),
)
Index(
    "ix_assessment_consensus_schema_target",
    assessment_consensus.c.schema_id,
    assessment_consensus.c.target_kind,
    assessment_consensus.c.target_id,
)
Index(
    "ix_assessment_realized_schema_target",
    assessment_realized_targets.c.schema_id,
    assessment_realized_targets.c.target_kind,
    assessment_realized_targets.c.target_id,
)
Index(
    "ix_assessment_realized_schema_horizon_status_target_round_output",
    assessment_realized_targets.c.schema_id,
    assessment_realized_targets.c.horizon_value,
    assessment_realized_targets.c.target_kind,
    assessment_realized_targets.c.status,
    assessment_realized_targets.c.output,
    assessment_realized_targets.c.target_id,
    assessment_realized_targets.c.round_id,
    assessment_realized_targets.c.value_text,
)
Index(
    "ix_assessment_scores_schema_miner",
    assessment_output_scores.c.schema_id,
    assessment_output_scores.c.miner_hotkey,
)
Index(
    "ix_assessment_state_schema_miner",
    assessment_miner_score_state.c.schema_id,
    assessment_miner_score_state.c.miner_hotkey,
)
Index(
    "ix_assessment_history_schema_miner",
    assessment_score_history.c.schema_id,
    assessment_score_history.c.miner_hotkey,
)
