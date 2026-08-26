"""Axon submission handlers: synapse → verdict → storage (spec §6)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime
from unittest.mock import patch

from endure.assessment.registry import (
    SchemaRegistry,
    SchemaRegistryEntry,
    UniverseSnapshot,
    default_registry,
)
from endure.assessment.schemas.forge_lending import (
    FORGE_LENDING_SCHEMA_ID,
    LENDING_HORIZON_SECONDS,
    ONE_E18,
    LendingSubmissionBundle,
)
from endure.assessment.schemas.subnet_alpha_risk import (
    RiskSubmissionBundle,
    build_risk_v1_subnet_alpha_schema,
)
from endure.protocol.canonical import (
    COMMIT_NONCE_BYTES,
    canonical_bundle_bytes,
    commit_hash,
)
from endure.protocol.handlers import SubmissionHandlers
from endure.protocol.round_engine import DEFAULT_OFFSETS, compute_windows
from endure.protocol.synapses import RejectionCode, SubmitCommit, SubmitReveal
from endure.protocol.version_contract import CURRENT_VERSION_KEY
from endure.storage.repository import Storage

ROUND = "2026-06-09"
VALID_NONCE_HEX = "01" * COMMIT_NONCE_BYTES
IN_COMMIT = datetime(2026, 6, 9, 15, 0, tzinfo=UTC)
IN_REVEAL = datetime(2026, 6, 9, 21, 0, tzinfo=UTC)


def _handlers(storage: Storage, now: datetime) -> SubmissionHandlers:
    return SubmissionHandlers(
        storage=storage,
        schema_id=FORGE_LENDING_SCHEMA_ID,
        now_fn=lambda: now,
        max_commits_per_round=3,
        max_reveals_per_round=10,
    )


def _open_round(storage: Storage, members: tuple[str, ...] = ("30",)) -> None:
    windows = compute_windows(date(2026, 6, 9), offsets=DEFAULT_OFFSETS)
    storage.open_round(
        windows=windows,
        schema_id=FORGE_LENDING_SCHEMA_ID,
        universe=UniverseSnapshot(round_id=ROUND, tickers=members, source_hash="h"),
        now_iso=IN_COMMIT.isoformat(),
    )


def _open_round_without_universe(storage: Storage) -> None:
    windows = compute_windows(date(2026, 6, 9), offsets=DEFAULT_OFFSETS)
    storage.open_round(
        windows=windows,
        schema_id=FORGE_LENDING_SCHEMA_ID,
        universe=None,
        now_iso=IN_COMMIT.isoformat(),
    )


def _lending_output(output: str, value: int, unit: str) -> dict[str, object]:
    return {
        "output": output,
        "value": value,
        "confidence_bps": 8000,
        "reason_codes": ["thin_liquidity"],
        "horizon_seconds": LENDING_HORIZON_SECONDS,
        "unit": unit,
    }


def _bundle_json(netuid: int = 30) -> str:
    payload = {
        "round_id": ROUND,
        "schema_id": FORGE_LENDING_SCHEMA_ID,
        "assets": [
            {
                "netuid": netuid,
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
    bundle = LendingSubmissionBundle.model_validate(payload)
    return canonical_bundle_bytes(bundle.to_canonical_payload()).decode()


def _commit_synapse(
    bundle_hash: str, schema_id: str = FORGE_LENDING_SCHEMA_ID
) -> SubmitCommit:
    return SubmitCommit(
        round_id=ROUND,
        schema_id=schema_id,
        spec_version=CURRENT_VERSION_KEY,
        bundle_hash=bundle_hash,
    )


class TestHandleCommit:
    async def test_accepts_and_persists(self, storage: Storage) -> None:
        _open_round(storage)
        handlers = _handlers(storage, IN_COMMIT)

        response = await handlers.handle_commit(
            _commit_synapse("ab" * 32), miner_hotkey="hk-a"
        )

        assert response.accepted is True
        assert (
            storage.committed_hash(ROUND, FORGE_LENDING_SCHEMA_ID, "hk-a") == "ab" * 32
        )

    async def test_rejects_when_no_round_open(self, storage: Storage) -> None:
        handlers = _handlers(storage, IN_COMMIT)

        response = await handlers.handle_commit(
            _commit_synapse("ab" * 32), miner_hotkey="hk-a"
        )

        assert response.accepted is False
        assert response.rejection_code == RejectionCode.ROUND_UNAVAILABLE.value

    async def test_oversized_round_id_rejected_before_storage(
        self, storage: Storage
    ) -> None:
        """An unbounded round_id must be capped before any DB query — no
        round is open, so without the guard this hits round_windows with a
        multi-megabyte parameter."""
        handlers = _handlers(storage, IN_COMMIT)

        response = await handlers.handle_commit(
            SubmitCommit(
                round_id="A" * 100_000,
                schema_id=FORGE_LENDING_SCHEMA_ID,
                spec_version=CURRENT_VERSION_KEY,
                bundle_hash="ab" * 32,
            ),
            miner_hotkey="hk-a",
        )

        assert response.rejection_code == RejectionCode.MALFORMED_BUNDLE.value

    async def test_non_canonical_round_id_rejected_before_storage(
        self, storage: Storage
    ) -> None:
        handlers = _handlers(storage, IN_COMMIT)

        response = await handlers.handle_commit(
            SubmitCommit(
                round_id="20260609",
                schema_id=FORGE_LENDING_SCHEMA_ID,
                spec_version=CURRENT_VERSION_KEY,
                bundle_hash="ab" * 32,
            ),
            miner_hotkey="hk-a",
        )

        assert response.rejection_code == RejectionCode.MALFORMED_BUNDLE.value

    async def test_over_cap_commit_short_circuits_before_validation(
        self, storage: Storage
    ) -> None:
        """The rate cap is also a DoS control: an over-cap miner gets
        RATE_LIMITED without burning validation work — even for payloads
        that would otherwise fail validation with a different code."""
        _open_round(storage)
        handlers = _handlers(storage, IN_COMMIT)
        for bundle_hash in ("ab" * 32, "cd" * 32, "ef" * 32):
            await handlers.handle_commit(
                _commit_synapse(bundle_hash), miner_hotkey="hk-a"
            )

        malformed = await handlers.handle_commit(
            _commit_synapse("not-hex"), miner_hotkey="hk-a"
        )

        assert malformed.rejection_code == RejectionCode.RATE_LIMITED.value

    async def test_atomic_cap_rejects_when_record_commit_returns_false(
        self, storage: Storage, monkeypatch
    ) -> None:
        """The advisory pre-check can pass yet the atomic record_commit still
        reject under a concurrent-commit race — that False must surface as
        RATE_LIMITED (handlers.py authoritative branch), not a silent accept."""
        _open_round(storage)
        handlers = _handlers(storage, IN_COMMIT)
        monkeypatch.setattr(storage, "record_commit", lambda *a, **k: False)

        response = await handlers.handle_commit(
            _commit_synapse("ab" * 32), miner_hotkey="hk-a"
        )

        assert response.accepted is False
        assert response.rejection_code == RejectionCode.RATE_LIMITED.value

    async def test_rate_limits_different_hash_recommits(self, storage: Storage) -> None:
        _open_round(storage)
        handlers = _handlers(storage, IN_COMMIT)
        for bundle_hash in ("ab" * 32, "cd" * 32, "ef" * 32):
            await handlers.handle_commit(
                _commit_synapse(bundle_hash), miner_hotkey="hk-a"
            )

        response = await handlers.handle_commit(
            _commit_synapse("01" * 32), miner_hotkey="hk-a"
        )

        assert response.rejection_code == RejectionCode.RATE_LIMITED.value
        assert (
            storage.committed_hash(ROUND, FORGE_LENDING_SCHEMA_ID, "hk-a") == "ef" * 32
        )
        assert storage.commit_count(ROUND, FORGE_LENDING_SCHEMA_ID, "hk-a") == 3

    async def test_accepts_identical_recommit_at_cap_without_consuming_slot(
        self, storage: Storage
    ) -> None:
        _open_round(storage)
        handlers = _handlers(storage, IN_COMMIT)
        for bundle_hash in ("ab" * 32, "cd" * 32, "ef" * 32):
            await handlers.handle_commit(
                _commit_synapse(bundle_hash), miner_hotkey="hk-a"
            )

        response = await handlers.handle_commit(
            _commit_synapse("ef" * 32), miner_hotkey="hk-a"
        )

        assert response.accepted is True
        assert storage.commit_count(ROUND, FORGE_LENDING_SCHEMA_ID, "hk-a") == 3


class TestSchemaMismatch:
    """A handler serves exactly its configured schema: a synapse for another
    registered schema must be rejected, never validated as one schema and
    stored under the other."""

    @staticmethod
    def _two_schema_registry() -> SchemaRegistry:
        registry = default_registry()
        registry.register(
            SchemaRegistryEntry(
                schema=dataclasses.replace(
                    build_risk_v1_subnet_alpha_schema(), schema_id="risk.other.v0"
                ),
                bundle_model=RiskSubmissionBundle,
                serving_status="registered_unserved",
            )
        )
        return registry

    async def test_commit_for_other_schema_is_rejected(self, storage: Storage) -> None:
        _open_round(storage)
        handlers = SubmissionHandlers(
            storage=storage,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            now_fn=lambda: IN_COMMIT,
            max_commits_per_round=3,
            registry=self._two_schema_registry(),
        )

        response = await handlers.handle_commit(
            SubmitCommit(
                round_id=ROUND,
                schema_id="risk.other.v0",
                spec_version=CURRENT_VERSION_KEY,
                bundle_hash="ab" * 32,
            ),
            miner_hotkey="hk-a",
        )

        assert response.accepted is False
        assert response.rejection_code == RejectionCode.UNKNOWN_SCHEMA.value
        assert storage.committed_hash(ROUND, FORGE_LENDING_SCHEMA_ID, "hk-a") is None

    async def test_reveal_for_other_schema_is_rejected(self, storage: Storage) -> None:
        _open_round(storage)
        handlers = SubmissionHandlers(
            storage=storage,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            now_fn=lambda: IN_REVEAL,
            max_commits_per_round=3,
            registry=self._two_schema_registry(),
        )

        response = await handlers.handle_reveal(
            SubmitReveal(
                round_id=ROUND,
                schema_id="risk.other.v0",
                spec_version=CURRENT_VERSION_KEY,
                bundle_json=_bundle_json(),
                nonce_hex=VALID_NONCE_HEX,
            ),
            miner_hotkey="hk-a",
        )

        assert response.accepted is False
        assert response.rejection_code == RejectionCode.UNKNOWN_SCHEMA.value
        assert storage.accepted_bundles(ROUND, FORGE_LENDING_SCHEMA_ID) == []


class TestHandleReveal:
    async def test_reveal_when_round_is_missing_is_rejected(
        self, storage: Storage
    ) -> None:
        response = await _handlers(storage, IN_REVEAL).handle_reveal(
            SubmitReveal(
                round_id=ROUND,
                schema_id=FORGE_LENDING_SCHEMA_ID,
                spec_version=CURRENT_VERSION_KEY,
                bundle_json=_bundle_json(),
                nonce_hex=VALID_NONCE_HEX,
            ),
            miner_hotkey="hk-a",
        )

        assert response.accepted is False
        assert response.rejection_code == RejectionCode.ROUND_UNAVAILABLE.value
        assert storage.accepted_bundles(ROUND, FORGE_LENDING_SCHEMA_ID) == []

    async def test_reveal_when_universe_is_missing_is_rejected(
        self, storage: Storage
    ) -> None:
        _open_round_without_universe(storage)
        bundle_json = _bundle_json()
        nonce = VALID_NONCE_HEX
        digest = commit_hash(
            bundle_json.encode(), bytes.fromhex(nonce), miner_hotkey="hk-a"
        )
        await _handlers(storage, IN_COMMIT).handle_commit(
            _commit_synapse(digest), miner_hotkey="hk-a"
        )

        response = await _handlers(storage, IN_REVEAL).handle_reveal(
            SubmitReveal(
                round_id=ROUND,
                schema_id=FORGE_LENDING_SCHEMA_ID,
                spec_version=CURRENT_VERSION_KEY,
                bundle_json=bundle_json,
                nonce_hex=nonce,
            ),
            miner_hotkey="hk-a",
        )

        assert response.accepted is False
        assert response.rejection_code == RejectionCode.ROUND_UNAVAILABLE.value
        assert storage.accepted_bundles(ROUND, FORGE_LENDING_SCHEMA_ID) == []

    async def test_accepts_matching_reveal_and_persists(self, storage: Storage) -> None:
        _open_round(storage)
        bundle_json = _bundle_json()
        nonce = VALID_NONCE_HEX
        digest = commit_hash(
            bundle_json.encode(), bytes.fromhex(nonce), miner_hotkey="hk-a"
        )
        await _handlers(storage, IN_COMMIT).handle_commit(
            _commit_synapse(digest), miner_hotkey="hk-a"
        )

        response = await _handlers(storage, IN_REVEAL).handle_reveal(
            SubmitReveal(
                round_id=ROUND,
                schema_id=FORGE_LENDING_SCHEMA_ID,
                spec_version=CURRENT_VERSION_KEY,
                bundle_json=bundle_json,
                nonce_hex=nonce,
            ),
            miner_hotkey="hk-a",
        )

        assert response.accepted is True
        assert storage.accepted_bundles(ROUND, FORGE_LENDING_SCHEMA_ID) == [
            ("hk-a", bundle_json)
        ]

    async def test_identical_reveal_retries_skip_revalidation(
        self, storage: Storage
    ) -> None:
        _open_round(storage)
        bundle_json = _bundle_json()
        nonce = VALID_NONCE_HEX
        digest = commit_hash(
            bundle_json.encode(), bytes.fromhex(nonce), miner_hotkey="hk-a"
        )
        await _handlers(storage, IN_COMMIT).handle_commit(
            _commit_synapse(digest), miner_hotkey="hk-a"
        )
        handlers = _handlers(storage, IN_REVEAL)
        reveal = SubmitReveal(
            round_id=ROUND,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            spec_version=CURRENT_VERSION_KEY,
            bundle_json=bundle_json,
            nonce_hex=nonce,
        )
        assert (await handlers.handle_reveal(reveal, miner_hotkey="hk-a")).accepted

        with patch(
            "endure.protocol.handlers.validate_reveal",
            side_effect=AssertionError("idempotent retry was revalidated"),
        ):
            retry = await handlers.handle_reveal(
                reveal.model_copy(), miner_hotkey="hk-a"
            )

        assert retry.accepted is True

    async def test_idempotent_retry_still_rejects_stale_protocol_version(
        self, storage: Storage
    ) -> None:
        _open_round(storage)
        bundle_json = _bundle_json()
        nonce = VALID_NONCE_HEX
        digest = commit_hash(
            bundle_json.encode(), bytes.fromhex(nonce), miner_hotkey="hk-a"
        )
        await _handlers(storage, IN_COMMIT).handle_commit(
            _commit_synapse(digest), miner_hotkey="hk-a"
        )
        handlers = _handlers(storage, IN_REVEAL)
        reveal = SubmitReveal(
            round_id=ROUND,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            spec_version=CURRENT_VERSION_KEY,
            bundle_json=bundle_json,
            nonce_hex=nonce,
        )
        assert (await handlers.handle_reveal(reveal, miner_hotkey="hk-a")).accepted

        stale = reveal.model_copy(update={"spec_version": CURRENT_VERSION_KEY - 1})

        retry = await handlers.handle_reveal(stale, miner_hotkey="hk-a")

        assert retry.accepted is False
        assert retry.rejection_code == RejectionCode.VERSION_MISMATCH.value

    async def test_reveal_without_commit_does_not_spend_persistent_budget(
        self, storage: Storage
    ) -> None:
        _open_round(storage)
        handlers = _handlers(storage, IN_REVEAL)

        await handlers.handle_reveal(
            SubmitReveal(
                round_id=ROUND,
                schema_id=FORGE_LENDING_SCHEMA_ID,
                spec_version=CURRENT_VERSION_KEY,
                bundle_json=_bundle_json(),
                nonce_hex=VALID_NONCE_HEX,
            ),
            miner_hotkey="hk-a",
        )

        assert storage.reveal_count(ROUND, FORGE_LENDING_SCHEMA_ID, "hk-a") == 0

    async def test_new_reveals_are_rate_limited_per_round(
        self, storage: Storage
    ) -> None:
        _open_round(storage)
        bundle_json = _bundle_json()
        valid_nonce = VALID_NONCE_HEX
        digest = commit_hash(
            bundle_json.encode(), bytes.fromhex(valid_nonce), miner_hotkey="hk-a"
        )
        await _handlers(storage, IN_COMMIT).handle_commit(
            _commit_synapse(digest), miner_hotkey="hk-a"
        )
        handlers = SubmissionHandlers(
            storage=storage,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            now_fn=lambda: IN_REVEAL,
            max_commits_per_round=3,
            max_reveals_per_round=2,
        )
        invalid_nonce = "02" * COMMIT_NONCE_BYTES
        reveal = SubmitReveal(
            round_id=ROUND,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            spec_version=CURRENT_VERSION_KEY,
            bundle_json=bundle_json,
            nonce_hex=invalid_nonce,
        )

        first = await handlers.handle_reveal(reveal, miner_hotkey="hk-a")
        second = await handlers.handle_reveal(reveal.model_copy(), miner_hotkey="hk-a")
        third = await handlers.handle_reveal(reveal.model_copy(), miner_hotkey="hk-a")

        assert first.rejection_code == RejectionCode.HASH_MISMATCH.value
        assert second.rejection_code == RejectionCode.HASH_MISMATCH.value
        assert third.rejection_code == RejectionCode.RATE_LIMITED.value

    async def test_rate_limited_oversized_reveal_never_reaches_disk(
        self, storage: Storage
    ) -> None:
        from sqlalchemy import select

        from endure.protocol.validation import MAX_REVEAL_BUNDLE_BYTES
        from endure.storage.tables import submissions

        _open_round(storage)
        bundle_json = _bundle_json()
        digest = commit_hash(
            bundle_json.encode(), bytes.fromhex(VALID_NONCE_HEX), miner_hotkey="hk-a"
        )
        await _handlers(storage, IN_COMMIT).handle_commit(
            _commit_synapse(digest), miner_hotkey="hk-a"
        )
        handlers = SubmissionHandlers(
            storage=storage,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            now_fn=lambda: IN_REVEAL,
            max_commits_per_round=3,
            max_reveals_per_round=1,
        )
        rejected = SubmitReveal(
            round_id=ROUND,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            spec_version=CURRENT_VERSION_KEY,
            bundle_json=bundle_json,
            nonce_hex="02" * COMMIT_NONCE_BYTES,
        )
        first = await handlers.handle_reveal(rejected, miner_hotkey="hk-a")
        oversized = rejected.model_copy(
            update={"bundle_json": '{"pad":"' + "x" * MAX_REVEAL_BUNDLE_BYTES + '"}'}
        )

        response = await handlers.handle_reveal(oversized, miner_hotkey="hk-a")

        assert first.rejection_code == RejectionCode.HASH_MISMATCH.value
        assert response.rejection_code == RejectionCode.PAYLOAD_TOO_LARGE.value
        with storage._engine.connect() as connection:
            row = connection.execute(
                select(submissions.c.bundle_json).where(
                    submissions.c.round_id == ROUND,
                    submissions.c.miner_hotkey == "hk-a",
                )
            ).one()
        assert row.bundle_json == bundle_json

    async def test_reveal_budget_survives_handler_restart(
        self, storage: Storage
    ) -> None:
        _open_round(storage)
        bundle_json = _bundle_json()
        digest = commit_hash(
            bundle_json.encode(), bytes.fromhex(VALID_NONCE_HEX), miner_hotkey="hk-a"
        )
        await _handlers(storage, IN_COMMIT).handle_commit(
            _commit_synapse(digest), miner_hotkey="hk-a"
        )
        first_handler = SubmissionHandlers(
            storage=storage,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            now_fn=lambda: IN_REVEAL,
            max_commits_per_round=3,
            max_reveals_per_round=2,
        )
        invalid = SubmitReveal(
            round_id=ROUND,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            spec_version=CURRENT_VERSION_KEY,
            bundle_json=bundle_json,
            nonce_hex="02" * COMMIT_NONCE_BYTES,
        )

        assert (
            await first_handler.handle_reveal(invalid, miner_hotkey="hk-a")
        ).rejection_code == RejectionCode.HASH_MISMATCH.value
        assert (
            await first_handler.handle_reveal(invalid.model_copy(), miner_hotkey="hk-a")
        ).rejection_code == RejectionCode.HASH_MISMATCH.value

        restarted_handler = SubmissionHandlers(
            storage=storage,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            now_fn=lambda: IN_REVEAL,
            max_commits_per_round=3,
            max_reveals_per_round=2,
        )
        third = await restarted_handler.handle_reveal(
            invalid.model_copy(), miner_hotkey="hk-a"
        )

        assert third.rejection_code == RejectionCode.RATE_LIMITED.value

    async def test_replayed_preimage_from_other_hotkey_is_rejected(
        self, storage: Storage
    ) -> None:
        """A hotkey that copied another miner's commit hash cannot reveal the
        stolen bundle+nonce — the preimage is bound to the original hotkey."""
        _open_round(storage)
        bundle_json = _bundle_json()
        nonce = VALID_NONCE_HEX
        stolen = commit_hash(
            bundle_json.encode(), bytes.fromhex(nonce), miner_hotkey="hk-a"
        )
        await _handlers(storage, IN_COMMIT).handle_commit(
            _commit_synapse(stolen), miner_hotkey="hk-b"
        )

        response = await _handlers(storage, IN_REVEAL).handle_reveal(
            SubmitReveal(
                round_id=ROUND,
                schema_id=FORGE_LENDING_SCHEMA_ID,
                spec_version=CURRENT_VERSION_KEY,
                bundle_json=bundle_json,
                nonce_hex=nonce,
            ),
            miner_hotkey="hk-b",
        )

        assert response.accepted is False
        assert response.rejection_code == RejectionCode.HASH_MISMATCH.value
        assert storage.accepted_bundles(ROUND, FORGE_LENDING_SCHEMA_ID) == []

    async def test_rejected_retry_cannot_downgrade_accepted_reveal(
        self, storage: Storage
    ) -> None:
        """An accepted reveal is terminal — a miner's own late retry must not
        erase it before 5d/30d scoring reads accepted_bundles."""
        _open_round(storage)
        bundle_json = _bundle_json()
        nonce = VALID_NONCE_HEX
        digest = commit_hash(
            bundle_json.encode(), bytes.fromhex(nonce), miner_hotkey="hk-a"
        )
        await _handlers(storage, IN_COMMIT).handle_commit(
            _commit_synapse(digest), miner_hotkey="hk-a"
        )
        reveal = SubmitReveal(
            round_id=ROUND,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            spec_version=CURRENT_VERSION_KEY,
            bundle_json=bundle_json,
            nonce_hex=nonce,
        )
        accepted = await _handlers(storage, IN_REVEAL).handle_reveal(
            reveal, miner_hotkey="hk-a"
        )
        assert accepted.accepted is True

        after_close = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
        retry = await _handlers(storage, after_close).handle_reveal(
            reveal.model_copy(), miner_hotkey="hk-a"
        )

        assert retry.accepted is False
        assert retry.rejection_code == RejectionCode.LATE_REVEAL.value
        assert storage.accepted_bundles(ROUND, FORGE_LENDING_SCHEMA_ID) == [
            ("hk-a", bundle_json)
        ]

    async def test_oversized_reveal_is_rejected_and_never_persisted(
        self, storage: Storage
    ) -> None:
        """Oversized payloads must not reach disk even as rejected audit rows
        — storing them would turn the size cap into a disk-exhaustion vector."""
        from sqlalchemy import select

        from endure.protocol.validation import MAX_REVEAL_BUNDLE_BYTES
        from endure.storage.tables import submissions

        _open_round(storage)
        bundle_json = _bundle_json()
        nonce = VALID_NONCE_HEX
        digest = commit_hash(
            bundle_json.encode(), bytes.fromhex(nonce), miner_hotkey="hk-a"
        )
        await _handlers(storage, IN_COMMIT).handle_commit(
            _commit_synapse(digest), miner_hotkey="hk-a"
        )

        oversized = '{"pad":"' + "x" * MAX_REVEAL_BUNDLE_BYTES + '"}'
        response = await _handlers(storage, IN_REVEAL).handle_reveal(
            SubmitReveal(
                round_id=ROUND,
                schema_id=FORGE_LENDING_SCHEMA_ID,
                spec_version=CURRENT_VERSION_KEY,
                bundle_json=oversized,
                nonce_hex=nonce,
            ),
            miner_hotkey="hk-a",
        )

        assert response.accepted is False
        assert response.rejection_code == RejectionCode.PAYLOAD_TOO_LARGE.value
        with storage._engine.connect() as connection:
            row = connection.execute(
                select(submissions.c.bundle_json, submissions.c.verdict).where(
                    submissions.c.round_id == ROUND,
                    submissions.c.miner_hotkey == "hk-a",
                )
            ).one()
        assert row.bundle_json is None
        assert row.verdict == "committed"

    async def test_oversized_reveal_is_rejected_before_storage_reads(
        self, storage: Storage
    ) -> None:
        from endure.protocol.validation import MAX_REVEAL_BUNDLE_BYTES

        handlers = _handlers(storage, IN_REVEAL)
        storage_read = patch.object(
            storage,
            "round_windows",
            side_effect=AssertionError("oversized payload reached storage"),
        )
        oversized = '{"pad":"' + "x" * MAX_REVEAL_BUNDLE_BYTES + '"}'

        with storage_read:
            response = await handlers.handle_reveal(
                SubmitReveal(
                    round_id=ROUND,
                    schema_id=FORGE_LENDING_SCHEMA_ID,
                    spec_version=CURRENT_VERSION_KEY,
                    bundle_json=oversized,
                    nonce_hex=VALID_NONCE_HEX,
                ),
                miner_hotkey="hk-a",
            )

        assert response.accepted is False
        assert response.rejection_code == RejectionCode.PAYLOAD_TOO_LARGE.value

    async def test_late_oversized_reveal_is_rejected_and_never_persisted(
        self, storage: Storage
    ) -> None:
        """The size cap must win before LATE_REVEAL so a committed miner cannot
        bypass bounded rejected-payload persistence after the window closes."""
        from sqlalchemy import select

        from endure.protocol.validation import MAX_REVEAL_BUNDLE_BYTES
        from endure.storage.tables import submissions

        _open_round(storage)
        oversized = '{"pad":"' + "x" * MAX_REVEAL_BUNDLE_BYTES + '"}'
        nonce = VALID_NONCE_HEX
        digest = commit_hash(
            oversized.encode(), bytes.fromhex(nonce), miner_hotkey="hk-a"
        )
        await _handlers(storage, IN_COMMIT).handle_commit(
            _commit_synapse(digest), miner_hotkey="hk-a"
        )

        after_close = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
        response = await _handlers(storage, after_close).handle_reveal(
            SubmitReveal(
                round_id=ROUND,
                schema_id=FORGE_LENDING_SCHEMA_ID,
                spec_version=CURRENT_VERSION_KEY,
                bundle_json=oversized,
                nonce_hex=nonce,
            ),
            miner_hotkey="hk-a",
        )

        assert response.accepted is False
        assert response.rejection_code == RejectionCode.PAYLOAD_TOO_LARGE.value
        with storage._engine.connect() as connection:
            row = connection.execute(
                select(submissions.c.bundle_json, submissions.c.verdict).where(
                    submissions.c.round_id == ROUND,
                    submissions.c.miner_hotkey == "hk-a",
                )
            ).one()
        assert row.bundle_json is None
        assert row.verdict == "committed"

    async def test_reveal_without_commit_is_rejected(self, storage: Storage) -> None:
        _open_round(storage)

        response = await _handlers(storage, IN_REVEAL).handle_reveal(
            SubmitReveal(
                round_id=ROUND,
                schema_id=FORGE_LENDING_SCHEMA_ID,
                spec_version=CURRENT_VERSION_KEY,
                bundle_json=_bundle_json(),
                nonce_hex=VALID_NONCE_HEX,
            ),
            miner_hotkey="hk-a",
        )

        assert response.rejection_code == RejectionCode.NO_COMMIT.value
        assert storage.accepted_bundles(ROUND, FORGE_LENDING_SCHEMA_ID) == []

    async def test_handler_uses_the_registry_bundle_model(
        self, storage: Storage
    ) -> None:
        _open_round(storage)
        bundle_json = _bundle_json()
        nonce = VALID_NONCE_HEX
        digest = commit_hash(
            bundle_json.encode(), bytes.fromhex(nonce), miner_hotkey="hk-a"
        )
        handlers = SubmissionHandlers(
            storage=storage,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            now_fn=lambda: IN_COMMIT,
            max_commits_per_round=3,
            registry=default_registry(),
        )
        await handlers.handle_commit(
            _commit_synapse(digest, schema_id=FORGE_LENDING_SCHEMA_ID),
            miner_hotkey="hk-a",
        )

        reveal_handlers = SubmissionHandlers(
            storage=storage,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            now_fn=lambda: IN_REVEAL,
            max_commits_per_round=3,
            registry=default_registry(),
        )
        response = await reveal_handlers.handle_reveal(
            SubmitReveal(
                round_id=ROUND,
                schema_id=FORGE_LENDING_SCHEMA_ID,
                spec_version=CURRENT_VERSION_KEY,
                bundle_json=bundle_json,
                nonce_hex=nonce,
            ),
            miner_hotkey="hk-a",
        )

        assert response.accepted is True
        assert storage.accepted_bundles(ROUND, FORGE_LENDING_SCHEMA_ID) == [
            ("hk-a", bundle_json)
        ]

    async def test_handler_rejects_universe_netuid_mismatch(
        self, storage: Storage
    ) -> None:
        _open_round(storage, members=("44",))
        bundle_json = _bundle_json(netuid=30)
        nonce = VALID_NONCE_HEX
        digest = commit_hash(
            bundle_json.encode(), bytes.fromhex(nonce), miner_hotkey="hk-a"
        )
        handlers = SubmissionHandlers(
            storage=storage,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            now_fn=lambda: IN_COMMIT,
            max_commits_per_round=3,
            registry=default_registry(),
        )
        await handlers.handle_commit(
            _commit_synapse(digest, schema_id=FORGE_LENDING_SCHEMA_ID),
            miner_hotkey="hk-a",
        )

        reveal_handlers = SubmissionHandlers(
            storage=storage,
            schema_id=FORGE_LENDING_SCHEMA_ID,
            now_fn=lambda: IN_REVEAL,
            max_commits_per_round=3,
            registry=default_registry(),
        )
        response = await reveal_handlers.handle_reveal(
            SubmitReveal(
                round_id=ROUND,
                schema_id=FORGE_LENDING_SCHEMA_ID,
                spec_version=CURRENT_VERSION_KEY,
                bundle_json=bundle_json,
                nonce_hex=nonce,
            ),
            miner_hotkey="hk-a",
        )

        assert response.accepted is False
        assert response.rejection_code == RejectionCode.INVALID_TICKER.value
        assert storage.accepted_bundles(ROUND, FORGE_LENDING_SCHEMA_ID) == []
