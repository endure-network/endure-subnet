"""Wire synapses + rejection codes (spec §6)."""

from __future__ import annotations

import bittensor as bt

from endure.protocol.synapses import RejectionCode, SubmitCommit, SubmitReveal


class TestRejectionCode:
    def test_stable_wire_strings(self) -> None:
        assert RejectionCode.VERSION_MISMATCH.value == "VERSION_MISMATCH"
        assert {code.value for code in RejectionCode} == {
            "VERSION_MISMATCH",
            "UNKNOWN_SCHEMA",
            "LATE_COMMIT",
            "NO_COMMIT",
            "LATE_REVEAL",
            "HASH_MISMATCH",
            "INVALID_TICKER",
            "OUT_OF_BOUNDS",
            "MALFORMED_BUNDLE",
            "PAYLOAD_TOO_LARGE",
            "RATE_LIMITED",
            "ROUND_UNAVAILABLE",
        }


class TestSubmitCommit:
    def test_is_a_synapse_with_commit_fields(self) -> None:
        synapse = SubmitCommit(
            round_id="2026-06-09",
            schema_id="risk.v1.subnet_alpha",
            spec_version=11,
            bundle_hash="ab" * 32,
        )

        assert isinstance(synapse, bt.Synapse)
        assert synapse.round_id == "2026-06-09"
        assert synapse.accepted is False
        assert synapse.rejection_code is None

    def test_validator_response_round_trips(self) -> None:
        synapse = SubmitCommit(
            round_id="2026-06-09",
            schema_id="risk.v1.subnet_alpha",
            spec_version=11,
            bundle_hash="ab" * 32,
        )
        synapse.accepted = False
        synapse.rejection_code = RejectionCode.LATE_COMMIT.value

        assert synapse.rejection_code == "LATE_COMMIT"


class TestSubmitReveal:
    def test_is_a_synapse_with_reveal_fields(self) -> None:
        synapse = SubmitReveal(
            round_id="2026-06-09",
            schema_id="risk.v1.subnet_alpha",
            spec_version=11,
            bundle_json='{"a":1}',
            nonce_hex="0102",
        )

        assert isinstance(synapse, bt.Synapse)
        assert synapse.bundle_json == '{"a":1}'
        assert synapse.accepted is False
