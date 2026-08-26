"""Commit/reveal verdict logic (spec §6) — pure, clock-free."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from endure.assessment.schemas.forge_lending import (
    FORGE_LENDING_SCHEMA_ID,
    LENDING_HORIZON_SECONDS,
    ONE_E18,
    LendingSubmissionBundle,
)
from endure.protocol.canonical import (
    COMMIT_NONCE_BYTES,
    canonical_bundle_bytes,
    commit_hash,
)
from endure.protocol.synapses import RejectionCode
from endure.protocol.validation import (
    MAX_NONCE_BYTES,
    MAX_REVEAL_BUNDLE_BYTES,
    validate_commit,
    validate_reveal,
)
from endure.protocol.version_contract import CURRENT_VERSION_KEY

COMMIT_OPEN = datetime(2026, 6, 9, 11, 0, tzinfo=UTC)
COMMIT_CLOSE = datetime(2026, 6, 9, 19, 30, tzinfo=UTC)
REVEAL_OPEN = datetime(2026, 6, 9, 20, 30, tzinfo=UTC)
REVEAL_CLOSE = datetime(2026, 6, 10, 0, 0, tzinfo=UTC)

UNIVERSE = ("30",)
VALID_NONCE_HEX = "01" * COMMIT_NONCE_BYTES


def _lending_output(output: str, value: int, unit: str) -> dict[str, object]:
    return {
        "output": output,
        "value": value,
        "confidence_bps": 8000,
        "reason_codes": ["thin_liquidity"],
        "horizon_seconds": LENDING_HORIZON_SECONDS,
        "unit": unit,
    }


def _payload() -> dict[str, object]:
    return {
        "round_id": "2026-06-09",
        "schema_id": FORGE_LENDING_SCHEMA_ID,
        "assets": [
            {
                "netuid": 30,
                "outputs": [
                    _lending_output("safe_asset_price", ONE_E18, "price_1e18"),
                    _lending_output("collateral_factor", 2500, "ltv_bps"),
                    _lending_output("liquidation_threshold", 3500, "ltv_bps"),
                    _lending_output(
                        "liquidation_incentive",
                        108 * 10**16,
                        "mantissa_1e18",
                    ),
                    _lending_output("supply_cap", 0, "underlying_units"),
                    _lending_output("borrow_cap", 0, "tao_units"),
                    _lending_output("risk_tier", 3, "ordinal"),
                ],
            }
        ],
    }


def _bundle_json(payload: dict[str, object] | None = None) -> str:
    if payload is not None:
        return canonical_bundle_bytes(payload).decode()
    bundle = LendingSubmissionBundle.model_validate(_payload())
    return canonical_bundle_bytes(bundle.to_canonical_payload()).decode()


def _commit(now: datetime, **overrides: object):
    arguments: dict[str, object] = {
        "round_id": "2026-06-09",
        "schema_id": FORGE_LENDING_SCHEMA_ID,
        "spec_version": CURRENT_VERSION_KEY,
        "bundle_hash": "ab" * 32,
        "now": now,
        "commit_open": COMMIT_OPEN,
        "commit_close": COMMIT_CLOSE,
    }
    arguments.update(overrides)
    return validate_commit(**arguments)  # type: ignore[arg-type]


def _reveal(now: datetime, **overrides: object):
    bundle_json = str(overrides.pop("bundle_json", _bundle_json()))
    nonce_hex = str(overrides.pop("nonce_hex", VALID_NONCE_HEX))
    committed = commit_hash(
        bundle_json.encode(), bytes.fromhex(nonce_hex), miner_hotkey="hk-a"
    )
    arguments: dict[str, object] = {
        "round_id": "2026-06-09",
        "schema_id": FORGE_LENDING_SCHEMA_ID,
        "spec_version": CURRENT_VERSION_KEY,
        "bundle_json": bundle_json,
        "nonce_hex": nonce_hex,
        "committed_hash": committed,
        "miner_hotkey": "hk-a",
        "now": now,
        "reveal_open": REVEAL_OPEN,
        "reveal_close": REVEAL_CLOSE,
        "universe": UNIVERSE,
    }
    arguments.update(overrides)
    return validate_reveal(**arguments)  # type: ignore[arg-type]


IN_COMMIT = datetime(2026, 6, 9, 15, 0, tzinfo=UTC)
IN_REVEAL = datetime(2026, 6, 9, 21, 0, tzinfo=UTC)


class TestValidateCommit:
    def test_accepts_within_window(self) -> None:
        verdict = _commit(IN_COMMIT)

        assert verdict.accepted is True
        assert verdict.rejection_code is None

    def test_rejects_version_mismatch(self) -> None:
        verdict = _commit(IN_COMMIT, spec_version=CURRENT_VERSION_KEY - 1)

        assert verdict.rejection_code is RejectionCode.VERSION_MISMATCH

    def test_rejects_unknown_schema(self) -> None:
        verdict = _commit(IN_COMMIT, schema_id="unknown.schema")

        assert verdict.rejection_code is RejectionCode.UNKNOWN_SCHEMA

    def test_rejects_oversized_round_id(self) -> None:
        verdict = _commit(IN_COMMIT, round_id="A" * 10_000)

        assert verdict.rejection_code is RejectionCode.MALFORMED_BUNDLE

    def test_rejects_after_deadline(self) -> None:
        late = datetime(2026, 6, 9, 19, 31, tzinfo=UTC)

        assert _commit(late).rejection_code is RejectionCode.LATE_COMMIT

    def test_rejects_malformed_hash(self) -> None:
        verdict = _commit(IN_COMMIT, bundle_hash="not-hex")

        assert verdict.rejection_code is RejectionCode.MALFORMED_BUNDLE

    def test_rejects_uppercase_hash(self) -> None:
        verdict = _commit(IN_COMMIT, bundle_hash="AB" * 32)

        assert verdict.rejection_code is RejectionCode.MALFORMED_BUNDLE

    def test_rejects_non_canonical_round_id(self) -> None:
        verdict = _commit(IN_COMMIT, round_id="20260609")

        assert verdict.rejection_code is RejectionCode.MALFORMED_BUNDLE


class TestValidateReveal:
    def test_accepts_and_parses_bundle(self) -> None:
        verdict = _reveal(IN_REVEAL)

        assert verdict.accepted is True
        assert verdict.bundle is not None
        assert verdict.bundle.schema_id == FORGE_LENDING_SCHEMA_ID

    def test_rejects_outside_window(self) -> None:
        early = datetime(2026, 6, 9, 20, 0, tzinfo=UTC)

        assert _reveal(early).rejection_code is RejectionCode.LATE_REVEAL

    def test_rejects_without_commit(self) -> None:
        verdict = _reveal(IN_REVEAL, committed_hash=None)

        assert verdict.rejection_code is RejectionCode.NO_COMMIT

    def test_rejects_hash_mismatch(self) -> None:
        verdict = _reveal(IN_REVEAL, committed_hash="cd" * 32)

        assert verdict.rejection_code is RejectionCode.HASH_MISMATCH

    def test_rejects_non_canonical_bundle_json(self) -> None:
        loose_json = '{"schema_id": "lending.v1.subnet_asset" }'

        verdict = _reveal(IN_REVEAL, bundle_json=loose_json)

        assert verdict.rejection_code is RejectionCode.MALFORMED_BUNDLE

    def test_rejects_oversized_bundle_before_hashing(self) -> None:
        """Size is checked before any hashing or parsing so adversarial
        payloads cost one length check, not CPU on the scoring path."""
        oversized = '{"pad":"' + "x" * MAX_REVEAL_BUNDLE_BYTES + '"}'

        verdict = _reveal(IN_REVEAL, bundle_json=oversized)

        assert verdict.rejection_code is RejectionCode.PAYLOAD_TOO_LARGE

    def test_rejects_huge_integer_bundle_json_without_raising(self) -> None:
        # A >4300-digit integer literal makes json.loads raise a plain
        # ValueError (CPython int-string limit), not JSONDecodeError.
        huge = '{"n":' + "1" * 5000 + "}"

        verdict = _reveal(IN_REVEAL, bundle_json=huge)

        assert verdict.rejection_code is RejectionCode.MALFORMED_BUNDLE

    def test_rejects_deeply_nested_bundle_json_without_raising(self) -> None:
        deep = "[" * 20_000 + "]" * 20_000

        verdict = _reveal(IN_REVEAL, bundle_json=deep)

        assert verdict.rejection_code is RejectionCode.MALFORMED_BUNDLE

    def test_rejects_surrogate_bundle_json_without_raising(self) -> None:
        # A lone surrogate cannot encode to UTF-8; the size check must reject
        # it rather than raise UnicodeEncodeError. No commit is needed to reach
        # the size check, so this is callable by any reveal.
        verdict = validate_reveal(
            round_id="2026-06-09",
            schema_id=FORGE_LENDING_SCHEMA_ID,
            spec_version=CURRENT_VERSION_KEY,
            bundle_json="\ud800",
            nonce_hex=VALID_NONCE_HEX,
            committed_hash="ab" * 32,
            miner_hotkey="hk-a",
            now=IN_REVEAL,
            reveal_open=REVEAL_OPEN,
            reveal_close=REVEAL_CLOSE,
            universe=UNIVERSE,
        )

        assert verdict.rejection_code is RejectionCode.MALFORMED_BUNDLE

    def test_rejects_oversized_nonce(self) -> None:
        verdict = _reveal(IN_REVEAL, nonce_hex="ab" * (MAX_NONCE_BYTES + 1))

        assert verdict.rejection_code is RejectionCode.PAYLOAD_TOO_LARGE

    def test_rejects_empty_nonce(self) -> None:
        verdict = _reveal(IN_REVEAL, nonce_hex="")

        assert verdict.rejection_code is RejectionCode.MALFORMED_BUNDLE

    def test_rejects_short_nonce(self) -> None:
        verdict = _reveal(IN_REVEAL, nonce_hex="ab" * (COMMIT_NONCE_BYTES - 1))

        assert verdict.rejection_code is RejectionCode.MALFORMED_BUNDLE

    def test_rejects_round_id_mismatch(self) -> None:
        verdict = _reveal(IN_REVEAL, round_id="2026-06-10")

        assert verdict.rejection_code is RejectionCode.MALFORMED_BUNDLE

    def test_rejects_well_formed_but_non_canonical_bundle_json(self) -> None:
        loose_json = json.dumps(_payload(), indent=2)
        committed = commit_hash(
            loose_json.encode(), bytes.fromhex(VALID_NONCE_HEX), miner_hotkey="hk-a"
        )

        verdict = _reveal(
            IN_REVEAL,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            bundle_json=loose_json,
            committed_hash=committed,
            universe=UNIVERSE,
        )

        assert verdict.rejection_code is RejectionCode.MALFORMED_BUNDLE

    def test_classifies_out_of_bounds_with_the_shared_error_code(self) -> None:
        payload = _payload()
        assets = payload["assets"]
        assert isinstance(assets, list)
        asset = assets[0]
        assert isinstance(asset, dict)
        outputs = asset["outputs"]
        assert isinstance(outputs, list)
        output = outputs[1]
        assert isinstance(output, dict)
        output["confidence_bps"] = 999

        verdict = _reveal(
            IN_REVEAL,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            bundle_json=_bundle_json(payload),
            universe=UNIVERSE,
        )

        assert verdict.rejection_code is RejectionCode.OUT_OF_BOUNDS

    def test_rejects_netuid_outside_frozen_universe(self) -> None:
        bundle_json = _bundle_json()
        committed = commit_hash(
            bundle_json.encode(), bytes.fromhex(VALID_NONCE_HEX), miner_hotkey="hk-a"
        )

        verdict = _reveal(
            IN_REVEAL,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            bundle_json=bundle_json,
            committed_hash=committed,
            universe=("44",),
        )

        assert verdict.rejection_code is RejectionCode.INVALID_TICKER
