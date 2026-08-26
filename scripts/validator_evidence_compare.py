"""Stable comparison helpers for validator evidence documents."""

from __future__ import annotations

import json

_WEIGHT_METADATA_FIELDS = (
    "schema_id",
    "round_id",
    "block",
    "min_allowed_weights",
    "max_weight_limit_text",
    "metagraph_size",
    "status",
    "submission_block",
    "confirmation_state",
    "confirmation_deadline_block",
    "baseline_last_update_block",
    "period_blocks",
    "chain_identity",
    "netuid",
    "submission_mode",
)
_ROUND_LOCAL_FIELDS = frozenset({"created_at"})


class InvalidEvidenceDocument(ValueError):
    __slots__ = ("detail",)

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


def _validate_document(document: dict[str, object]) -> None:
    for field in ("rounds", "emas", "weights"):
        if not isinstance(document.get(field), list):
            raise InvalidEvidenceDocument(f"{field} must be a list")
    if not isinstance(document.get("schema_id"), str):
        raise InvalidEvidenceDocument("schema_id must be a string")
    if not isinstance(document.get("feed_sha256"), str):
        raise InvalidEvidenceDocument("feed_sha256 must be a string")
    truncated = document.get("truncated")
    if truncated is not None and not isinstance(truncated, dict):
        raise InvalidEvidenceDocument("truncated must be an object")


def coordinate_key(payload: dict[str, object]) -> str:
    parts: list[str] = []
    target_kind = payload.get("target_kind")
    if target_kind not in (None, ""):
        parts.append(f"target_kind={target_kind}")
    parts.append(f"target_id={payload.get('target_id', '')}")
    horizon_kind = payload.get("horizon_kind")
    if horizon_kind not in (None, ""):
        parts.append(f"horizon_kind={horizon_kind}")
    horizon = payload.get("horizon_value")
    if horizon not in (None, ""):
        parts.append(f"horizon={horizon}")
    parts.append(f"output={payload.get('output', '')}")
    return "/".join(parts)


def _index_rows(
    rows: object,
    *,
    scope: str,
    round_id: str | None = None,
) -> dict[str, object]:
    if rows is None:
        return {}
    if not isinstance(rows, list):
        raise InvalidEvidenceDocument(f"{scope} must be a list")
    indexed: dict[str, object] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise InvalidEvidenceDocument(f"{scope} rows must be objects")
        if round_id is not None:
            key = coordinate_key(row)
        elif "miner_hotkey" in row:
            key = f"miner_hotkey={row.get('miner_hotkey', '')}/{coordinate_key(row)}"
        else:
            key = json.dumps(row, sort_keys=True, separators=(",", ":"))
        if key in indexed:
            raise InvalidEvidenceDocument(f"duplicate {scope} identity: {key}")
        indexed[key] = row
    return indexed


def _index_rounds(rows: object) -> dict[str, object]:
    if not isinstance(rows, list):
        raise InvalidEvidenceDocument("rounds must be a list")
    indexed: dict[str, object] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("round_id"), str):
            raise InvalidEvidenceDocument("round rows require a string round_id")
        round_id = str(row["round_id"])
        if round_id in indexed:
            raise InvalidEvidenceDocument(f"duplicate round identity: {round_id}")
        indexed[round_id] = row
    return indexed


def _weight_group_key(batch: dict[str, object]) -> str:
    round_id = batch.get("round_id")
    if round_id is not None:
        return f"round={round_id}"
    block = batch.get("block")
    if block is not None:
        return f"block={block}"
    return "unanchored"


def _index_weights(
    rows: object,
) -> tuple[dict[str, object], dict[str, object]]:
    if not isinstance(rows, list):
        raise InvalidEvidenceDocument("weights must be a list")
    batch_metadata: dict[str, object] = {}
    weight_rows: dict[str, object] = {}
    occurrences: dict[str, int] = {}
    for batch in rows:
        if not isinstance(batch, dict):
            raise InvalidEvidenceDocument("weight batches must be objects")
        group = _weight_group_key(batch)
        occurrence = occurrences.get(group, 0)
        occurrences[group] = occurrence + 1
        batch_key = f"{group}/occurrence={occurrence}"
        batch_metadata[batch_key] = {
            field: batch.get(field) for field in _WEIGHT_METADATA_FIELDS
        }
        batch_rows = batch.get("rows")
        if batch_rows is None:
            continue
        if not isinstance(batch_rows, list):
            raise InvalidEvidenceDocument("weight batch rows must be a list")
        for row in batch_rows:
            if not isinstance(row, dict) or "uid" not in row:
                raise InvalidEvidenceDocument("weight rows require a uid")
            row_key = f"{batch_key}/uid={row['uid']}"
            if row_key in weight_rows:
                raise InvalidEvidenceDocument(f"duplicate weights identity: {row_key}")
            weight_rows[row_key] = row
    return batch_metadata, weight_rows


def _compare_mapping(
    left: dict[str, object],
    right: dict[str, object],
    *,
    scope: str,
    round_id: str | None = None,
) -> list[dict[str, object]]:
    diffs: list[dict[str, object]] = []
    for key in sorted(set(left) | set(right)):
        left_present = key in left
        right_present = key in right
        if left_present != right_present or left.get(key) != right.get(key):
            diff: dict[str, object] = {
                "scope": scope,
                "key": key,
                "left": left.get(key),
                "right": right.get(key),
            }
            if left_present != right_present:
                diff["left_present"] = left_present
                diff["right_present"] = right_present
            if round_id is not None:
                diff["round_id"] = round_id
            diffs.append(diff)
    return diffs


def _compare_rounds(left: object, right: object) -> list[dict[str, object]]:
    diffs: list[dict[str, object]] = []
    left_rounds = _index_rounds(left)
    right_rounds = _index_rounds(right)
    for round_id in sorted(set(left_rounds) | set(right_rounds)):
        left_round = left_rounds.get(round_id, {})
        right_round = right_rounds.get(round_id, {})
        if not isinstance(left_round, dict) or not isinstance(right_round, dict):
            diffs.extend(
                _compare_mapping(
                    {"round": left_round},
                    {"round": right_round},
                    scope="round",
                    round_id=round_id,
                )
            )
            continue
        comparable_fields = (
            (set(left_round) | set(right_round))
            - _ROUND_LOCAL_FIELDS
            - {"targets", "consensus"}
        )
        diffs.extend(
            _compare_mapping(
                {
                    field: left_round[field]
                    for field in comparable_fields
                    if field in left_round
                },
                {
                    field: right_round[field]
                    for field in comparable_fields
                    if field in right_round
                },
                scope="round",
                round_id=round_id,
            )
        )
        for section in ("targets", "consensus"):
            diffs.extend(
                _compare_mapping(
                    _index_rows(
                        left_round.get(section), scope=section, round_id=round_id
                    ),
                    _index_rows(
                        right_round.get(section), scope=section, round_id=round_id
                    ),
                    scope=section,
                    round_id=round_id,
                )
            )
    return diffs


def compare_validator_evidence(
    left: dict[str, object], right: dict[str, object]
) -> list[dict[str, object]]:
    """Return stable, named differences between two evidence exports."""
    _validate_document(left)
    _validate_document(right)
    diffs = _compare_mapping(
        {"schema_id": left.get("schema_id"), "truncated": left.get("truncated")},
        {"schema_id": right.get("schema_id"), "truncated": right.get("truncated")},
        scope="metadata",
    )
    diffs.extend(_compare_rounds(left.get("rounds"), right.get("rounds")))
    diffs.extend(
        _compare_mapping(
            _index_rows(left.get("emas"), scope="emas"),
            _index_rows(right.get("emas"), scope="emas"),
            scope="emas",
        )
    )
    left_weight_batches, left_weight_rows = _index_weights(left.get("weights"))
    right_weight_batches, right_weight_rows = _index_weights(right.get("weights"))
    diffs.extend(
        _compare_mapping(left_weight_batches, right_weight_batches, scope="weights")
    )
    diffs.extend(_compare_mapping(left_weight_rows, right_weight_rows, scope="weights"))
    if left.get("feed_sha256") != right.get("feed_sha256"):
        diffs.append(
            {
                "scope": "feed",
                "key": "feed_sha256",
                "left": left.get("feed_sha256"),
                "right": right.get("feed_sha256"),
            }
        )
    return diffs
