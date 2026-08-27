"""Round/submission persistence (spec §5, §11)."""

from __future__ import annotations

import os
import stat
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import event, insert, select, update

from endure.assessment.coordinates import AssessmentConsensusRow
from endure.assessment.registry import UniverseSnapshot
from endure.assessment.schemas.subnet_alpha_risk import RISK_SCHEMA_ID
from endure.protocol.round_engine import DEFAULT_OFFSETS, compute_windows
from endure.storage.repository import Storage, ensure_sqlite_parent_dir
from endure.storage.tables import (
    assessment_consensus,
    consensus_bundle_snapshots,
    rounds,
    submissions,
)


class TestStorageConstruction:
    def test_from_url_rejects_non_sqlite_backend(self) -> None:
        """The store is SQLite-only — a non-SQLite URL must fail fast rather
        than run with the WAL/foreign-key pragmas silently skipped."""
        with pytest.raises(ValueError, match="SQLite"):
            Storage.from_url("postgresql://user:pass@localhost:5432/endure")


NOW = datetime(2026, 6, 9, 11, 0, tzinfo=UTC).isoformat()


def _open_round(storage: Storage) -> str:
    windows = compute_windows(date(2026, 6, 9), offsets=DEFAULT_OFFSETS)
    snapshot = UniverseSnapshot(
        round_id="2026-06-09", tickers=("WAL", "ZION"), source_hash="hash123"
    )
    storage.open_round(
        windows=windows, schema_id=RISK_SCHEMA_ID, universe=snapshot, now_iso=NOW
    )
    return windows.round_id


def _corrupt_round_state(storage: Storage, round_id: str, state: str) -> None:
    with storage._engine.begin() as connection:
        connection.execute(
            update(rounds)
            .where(rounds.c.round_id == round_id, rounds.c.schema_id == RISK_SCHEMA_ID)
            .values(state=state, updated_at=NOW)
        )


class TestRounds:
    def test_open_round_persists_state_and_universe(self, storage: Storage) -> None:
        round_id = _open_round(storage)

        assert storage.round_state(round_id, RISK_SCHEMA_ID) == "open"
        universe = storage.universe_for(round_id, RISK_SCHEMA_ID)
        assert universe is not None
        assert universe.tickers == ("WAL", "ZION")
        assert universe.source_hash == "hash123"

    def test_open_round_is_idempotent(self, storage: Storage) -> None:
        round_id = _open_round(storage)
        _open_round(storage)

        assert storage.round_state(round_id, RISK_SCHEMA_ID) == "open"

    def test_set_round_state(self, storage: Storage) -> None:
        round_id = _open_round(storage)

        storage.set_round_state(round_id, RISK_SCHEMA_ID, "revealed", now_iso=NOW)

        assert storage.round_state(round_id, RISK_SCHEMA_ID) == "revealed"

    def test_revealing_round_state_snapshots_accepted_bundles_for_scoring(
        self, storage: Storage
    ) -> None:
        round_id = _open_round(storage)
        storage.record_reveal(
            round_id,
            RISK_SCHEMA_ID,
            "hk-a",
            bundle_json='{"included":true}',
            nonce_hex="01",
            accepted=True,
            rejection_code=None,
            now_iso=NOW,
        )

        storage.set_round_state(round_id, RISK_SCHEMA_ID, "revealed", now_iso=NOW)

        assert storage.scoring_bundles(round_id, RISK_SCHEMA_ID) == [
            ("hk-a", '{"included":true}')
        ]

    def test_set_round_state_rejects_invalid_state(self, storage: Storage) -> None:
        round_id = _open_round(storage)

        with pytest.raises(ValueError, match="round state"):
            storage.set_round_state(round_id, RISK_SCHEMA_ID, "opne", now_iso=NOW)

        assert storage.round_state(round_id, RISK_SCHEMA_ID) == "open"

    def test_unknown_round_state_is_none(self, storage: Storage) -> None:
        assert storage.round_state("1999-01-01", RISK_SCHEMA_ID) is None

    def test_consensus_snapshot_excludes_reveal_persisted_after_publication(
        self, storage: Storage
    ) -> None:
        round_id = _open_round(storage)
        storage.record_reveal(
            round_id,
            RISK_SCHEMA_ID,
            "hk-in-consensus",
            bundle_json='{"included":true}',
            nonce_hex="01",
            accepted=True,
            rejection_code=None,
            now_iso=NOW,
        )

        captured: list[tuple[str, str]] = []

        def consensus_rows(
            bundles: list[tuple[str, str]],
        ) -> list[AssessmentConsensusRow]:
            captured.extend(bundles)
            return []

        storage.publish_assessment_consensus_from_accepted_bundles_and_reveal(
            round_id,
            RISK_SCHEMA_ID,
            consensus_rows,
            now_iso=NOW,
        )
        storage.record_reveal(
            round_id,
            RISK_SCHEMA_ID,
            "hk-late",
            bundle_json='{"excluded":true}',
            nonce_hex="02",
            accepted=True,
            rejection_code=None,
            now_iso=NOW,
        )

        assert captured == [("hk-in-consensus", '{"included":true}')]
        assert storage.accepted_bundles(round_id, RISK_SCHEMA_ID) == [
            ("hk-in-consensus", '{"included":true}'),
            ("hk-late", '{"excluded":true}'),
        ]
        assert storage.scoring_bundles(round_id, RISK_SCHEMA_ID) == [
            ("hk-in-consensus", '{"included":true}')
        ]

    def test_scoring_bundles_lazily_freezes_legacy_post_embargo_round(
        self, storage: Storage
    ) -> None:
        """A legacy revealed round snapshots its current accepted set on first read."""
        round_id = _open_round(storage)
        storage.record_reveal(
            round_id,
            RISK_SCHEMA_ID,
            "hk-initial",
            bundle_json='{"included":true}',
            nonce_hex="01",
            accepted=True,
            rejection_code=None,
            now_iso=NOW,
        )
        with storage._engine.begin() as connection:
            connection.execute(
                update(rounds)
                .where(
                    rounds.c.round_id == round_id,
                    rounds.c.schema_id == RISK_SCHEMA_ID,
                )
                .values(state="revealed", updated_at=NOW)
            )

        first_read = storage.scoring_bundles(
            round_id, RISK_SCHEMA_ID, now_iso="2026-06-09T12:00:00+00:00"
        )
        storage.record_reveal(
            round_id,
            RISK_SCHEMA_ID,
            "hk-late",
            bundle_json='{"excluded":true}',
            nonce_hex="02",
            accepted=True,
            rejection_code=None,
            now_iso=NOW,
        )

        with storage._engine.connect() as connection:
            snapshot_rows = connection.execute(
                select(
                    consensus_bundle_snapshots.c.miner_hotkey,
                    consensus_bundle_snapshots.c.snapshotted_at,
                ).where(
                    consensus_bundle_snapshots.c.round_id == round_id,
                    consensus_bundle_snapshots.c.schema_id == RISK_SCHEMA_ID,
                )
            ).all()

        assert first_read == [("hk-initial", '{"included":true}')]
        assert storage.scoring_bundles(round_id, RISK_SCHEMA_ID) == first_read
        assert snapshot_rows == [("hk-initial", "2026-06-09T12:00:00+00:00")]


class TestCommits:
    def test_record_and_read_commit(self, storage: Storage) -> None:
        round_id = _open_round(storage)

        storage.record_commit(
            round_id, RISK_SCHEMA_ID, "hotkey-a", "ab" * 32, now_iso=NOW
        )

        assert storage.committed_hash(round_id, RISK_SCHEMA_ID, "hotkey-a") == "ab" * 32

    def test_recommit_last_wins(self, storage: Storage) -> None:
        round_id = _open_round(storage)
        storage.record_commit(
            round_id, RISK_SCHEMA_ID, "hotkey-a", "ab" * 32, now_iso=NOW
        )

        storage.record_commit(
            round_id, RISK_SCHEMA_ID, "hotkey-a", "cd" * 32, now_iso=NOW
        )

        assert storage.committed_hash(round_id, RISK_SCHEMA_ID, "hotkey-a") == "cd" * 32

    def test_commit_count_for_rate_limiting(self, storage: Storage) -> None:
        round_id = _open_round(storage)
        for bundle_hash in ("ab" * 32, "cd" * 32, "ef" * 32):
            storage.record_commit(
                round_id, RISK_SCHEMA_ID, "hotkey-a", bundle_hash, now_iso=NOW
            )

        assert storage.commit_count(round_id, RISK_SCHEMA_ID, "hotkey-a") == 3
        assert storage.commit_count(round_id, RISK_SCHEMA_ID, "hotkey-b") == 0

    def test_record_commit_enforces_cap_atomically(self, storage: Storage) -> None:
        """Check-and-increment in one transaction; concurrent commits cannot
        exceed the cap."""
        round_id = _open_round(storage)
        verdicts = [
            storage.record_commit(
                round_id,
                RISK_SCHEMA_ID,
                "hotkey-a",
                f"{i:02d}" * 32,
                now_iso=NOW,
                max_commits=3,
            )
            for i in range(4)
        ]

        assert verdicts == [True, True, True, False]
        assert storage.commit_count(round_id, RISK_SCHEMA_ID, "hotkey-a") == 3
        # The rejected fourth commit must not have replaced the hash.
        assert storage.committed_hash(round_id, RISK_SCHEMA_ID, "hotkey-a") == "02" * 32

    def test_identical_recommit_at_cap_is_accepted_without_incrementing(
        self, storage: Storage
    ) -> None:
        round_id = _open_round(storage)
        for bundle_hash in ("ab" * 32, "cd" * 32, "ef" * 32):
            assert storage.record_commit(
                round_id,
                RISK_SCHEMA_ID,
                "hotkey-a",
                bundle_hash,
                now_iso=NOW,
                max_commits=3,
            )

        accepted = storage.record_commit(
            round_id,
            RISK_SCHEMA_ID,
            "hotkey-a",
            "ef" * 32,
            now_iso=NOW,
            max_commits=3,
        )

        assert accepted is True
        assert storage.commit_count(round_id, RISK_SCHEMA_ID, "hotkey-a") == 3

    def test_record_commit_cap_holds_under_concurrency(self, storage: Storage) -> None:
        from concurrent.futures import ThreadPoolExecutor

        round_id = _open_round(storage)

        def attempt(index: int) -> bool:
            return storage.record_commit(
                round_id,
                RISK_SCHEMA_ID,
                "hotkey-a",
                f"{index:02d}" * 32,
                now_iso=NOW,
                max_commits=5,
            )

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(attempt, range(10)))

        assert sum(results) == 5
        assert storage.commit_count(round_id, RISK_SCHEMA_ID, "hotkey-a") == 5


class TestReveals:
    def test_record_reveal_attempt_is_restart_surviving_and_capped(
        self, storage: Storage
    ) -> None:
        round_id = _open_round(storage)
        storage.record_commit(
            round_id, RISK_SCHEMA_ID, "hotkey-a", "ab" * 32, now_iso=NOW
        )

        assert storage.record_reveal_attempt(
            round_id, RISK_SCHEMA_ID, "hotkey-a", max_reveals=2
        )
        assert storage.record_reveal_attempt(
            round_id, RISK_SCHEMA_ID, "hotkey-a", max_reveals=2
        )
        assert not storage.record_reveal_attempt(
            round_id, RISK_SCHEMA_ID, "hotkey-a", max_reveals=2
        )

        restarted = Storage.from_url(str(storage._engine.url))
        assert restarted.reveal_count(round_id, RISK_SCHEMA_ID, "hotkey-a") == 2

    def test_record_reveal_attempt_cap_holds_under_concurrency(
        self, storage: Storage
    ) -> None:
        from concurrent.futures import ThreadPoolExecutor

        round_id = _open_round(storage)
        storage.record_commit(
            round_id, RISK_SCHEMA_ID, "hotkey-a", "ab" * 32, now_iso=NOW
        )

        def attempt(_: int) -> bool:
            return storage.record_reveal_attempt(
                round_id, RISK_SCHEMA_ID, "hotkey-a", max_reveals=5
            )

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(attempt, range(10)))

        assert sum(results) == 5
        assert storage.reveal_count(round_id, RISK_SCHEMA_ID, "hotkey-a") == 5

    def test_accepted_reveal_round_trips(self, storage: Storage) -> None:
        round_id = _open_round(storage)
        storage.record_commit(
            round_id, RISK_SCHEMA_ID, "hotkey-a", "ab" * 32, now_iso=NOW
        )

        storage.record_reveal(
            round_id,
            RISK_SCHEMA_ID,
            "hotkey-a",
            bundle_json='{"a":1}',
            nonce_hex="0102",
            accepted=True,
            rejection_code=None,
            now_iso=NOW,
        )

        bundles = storage.accepted_bundles(round_id, RISK_SCHEMA_ID)
        assert bundles == [("hotkey-a", '{"a":1}')]

    def test_rejected_reveal_is_not_listed_as_accepted(self, storage: Storage) -> None:
        round_id = _open_round(storage)
        storage.record_commit(
            round_id, RISK_SCHEMA_ID, "hotkey-a", "ab" * 32, now_iso=NOW
        )

        storage.record_reveal(
            round_id,
            RISK_SCHEMA_ID,
            "hotkey-a",
            bundle_json='{"a":1}',
            nonce_hex="0102",
            accepted=False,
            rejection_code="HASH_MISMATCH",
            now_iso=NOW,
        )

        assert storage.accepted_bundles(round_id, RISK_SCHEMA_ID) == []

    def test_conflicted_rejected_reveal_reports_no_persistence(
        self, storage: Storage
    ) -> None:
        round_id = _open_round(storage)
        competing_reveal_written = False

        def write_competing_reveal_before_insert(
            _connection,
            _cursor,
            statement: str,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            nonlocal competing_reveal_written
            if competing_reveal_written or not statement.startswith(
                "INSERT INTO submissions"
            ):
                return
            competing_reveal_written = True
            with storage._engine.begin() as competing_connection:
                competing_connection.execute(
                    insert(submissions).values(
                        round_id=round_id,
                        schema_id=RISK_SCHEMA_ID,
                        miner_hotkey="hk",
                        revealed_at=NOW,
                        bundle_json='{"winner":true}',
                        nonce_hex="0102",
                        verdict="accepted",
                        rejection_code=None,
                    )
                )

        event.listen(
            storage._engine,
            "before_cursor_execute",
            write_competing_reveal_before_insert,
        )
        try:
            persisted = storage.record_reveal(
                round_id,
                RISK_SCHEMA_ID,
                "hk",
                bundle_json='{"loser":true}',
                nonce_hex="0103",
                accepted=False,
                rejection_code="HASH_MISMATCH",
                now_iso=NOW,
            )
        finally:
            event.remove(
                storage._engine,
                "before_cursor_execute",
                write_competing_reveal_before_insert,
            )

        assert competing_reveal_written is True
        assert persisted is False
        assert storage.accepted_bundles(round_id, RISK_SCHEMA_ID) == [
            ("hk", '{"winner":true}')
        ]

    def test_identical_reveal_repush_is_a_noop_write(self, storage: Storage) -> None:
        """A re-pushed identical reveal must not churn the row — the second
        store is a no-op (bounds reveal-attempt write DoS)."""
        round_id = _open_round(storage)
        storage.record_commit(round_id, RISK_SCHEMA_ID, "hk", "ab" * 32, now_iso=NOW)

        first = storage.record_reveal(
            round_id,
            RISK_SCHEMA_ID,
            "hk",
            bundle_json='{"a":1}',
            nonce_hex="0102",
            accepted=False,
            rejection_code="HASH_MISMATCH",
            now_iso=NOW,
        )
        repeat = storage.record_reveal(
            round_id,
            RISK_SCHEMA_ID,
            "hk",
            bundle_json='{"a":1}',
            nonce_hex="0102",
            accepted=False,
            rejection_code="HASH_MISMATCH",
            now_iso=NOW,
        )

        assert first is True
        assert repeat is False

    def test_corrected_reveal_with_new_content_is_persisted(
        self, storage: Storage
    ) -> None:
        """A genuine retry with different content (the miner fixed its bundle)
        is still written — only identical re-pushes are skipped."""
        round_id = _open_round(storage)
        storage.record_commit(round_id, RISK_SCHEMA_ID, "hk", "ab" * 32, now_iso=NOW)
        storage.record_reveal(
            round_id,
            RISK_SCHEMA_ID,
            "hk",
            bundle_json='{"a":1}',
            nonce_hex="0102",
            accepted=False,
            rejection_code="HASH_MISMATCH",
            now_iso=NOW,
        )

        rewrote = storage.record_reveal(
            round_id,
            RISK_SCHEMA_ID,
            "hk",
            bundle_json='{"a":2}',
            nonce_hex="0103",
            accepted=True,
            rejection_code=None,
            now_iso=NOW,
        )

        assert rewrote is True
        assert storage.accepted_bundles(round_id, RISK_SCHEMA_ID) == [("hk", '{"a":2}')]


class TestCoordinateRowMapping:
    """A corrupt persisted coordinate fails loudly instead of being coerced."""

    @staticmethod
    def _insert_consensus_row(
        storage: Storage,
        round_id: str,
        *,
        target_kind: str = "subnet_asset",
        horizon_kind: str = "seconds",
        n_submitters: object = 3,
    ) -> None:
        with storage._engine.begin() as connection:
            connection.execute(
                insert(assessment_consensus).values(
                    round_id=round_id,
                    schema_id=RISK_SCHEMA_ID,
                    target_kind=target_kind,
                    target_id="44",
                    horizon_kind=horizon_kind,
                    horizon_value=3600,
                    output="drawdown_bps",
                    value_text="1234",
                    dispersion_text="10",
                    n_submitters=n_submitters,
                    computed_at=NOW,
                )
            )

    @pytest.mark.parametrize(
        ("kinds", "message"),
        [
            ({"target_kind": "equity_ticker"}, None),
            ({"horizon_kind": "trading_days"}, None),
            ({"target_kind": "bogus"}, "unknown target_kind: bogus"),
            ({"horizon_kind": "bogus"}, "unknown horizon_kind: bogus"),
        ],
        ids=["ticker-kind", "trading-day-kind", "bad-target-kind", "bad-horizon-kind"],
    )
    def test_persisted_coordinate_kinds_round_trip_or_are_refused(
        self, storage: Storage, kinds: dict[str, str], message: str | None
    ) -> None:
        round_id = _open_round(storage)
        self._insert_consensus_row(storage, round_id, **kinds)

        if message is None:
            [row] = storage.assessment_consensus_for(round_id, RISK_SCHEMA_ID)
            assert row.n_submitters == 3
            return
        with pytest.raises(ValueError, match=message):
            storage.assessment_consensus_for(round_id, RISK_SCHEMA_ID)

    def test_non_integer_persisted_count_is_refused(self, storage: Storage) -> None:
        # SQLite's type affinity lets a non-integer land in an INTEGER column;
        # the row mapper must reject it rather than coerce a bogus count.
        round_id = _open_round(storage)
        self._insert_consensus_row(storage, round_id, n_submitters="three")

        with pytest.raises(TypeError, match="n_submitters must be integer"):
            storage.assessment_consensus_for(round_id, RISK_SCHEMA_ID)


class TestSqliteParentDirectory:
    def test_from_url_creates_missing_cwd_relative_parent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert not (tmp_path / "var").exists()

        Storage.from_url("sqlite:///var/endure.db")

        assert (tmp_path / "var").is_dir()
        assert stat.S_IMODE((tmp_path / "var").stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / "var" / "endure.db").stat().st_mode) == 0o600

    def test_from_url_creates_missing_absolute_parent(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "deeper" / "endure.db"

        Storage.from_url(f"sqlite:///{target}")

        assert target.parent.is_dir()
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_from_url_tightens_an_existing_database(self, tmp_path: Path) -> None:
        target = tmp_path / "endure.db"
        target.touch(mode=0o644)
        target.chmod(0o644)

        Storage.from_url(f"sqlite:///{target}")

        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_from_url_rejects_a_database_symlink(self, tmp_path: Path) -> None:
        target = tmp_path / "target.db"
        target.touch()
        link = tmp_path / "linked.db"
        link.symlink_to(target)

        with pytest.raises(ValueError, match="symbolic link"):
            Storage.from_url(f"sqlite:///{link}")

    @pytest.mark.parametrize(
        "url",
        (
            "sqlite://",
            "sqlite:///:memory:",
            "sqlite:///file::memory:?cache=shared&uri=true",
        ),
    )
    def test_memory_urls_touch_no_directory(
        self, url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        ensure_sqlite_parent_dir(url)

        assert list(tmp_path.iterdir()) == []

    def test_file_uri_database_is_owner_only(self, tmp_path: Path) -> None:
        target = tmp_path / "uri.db"

        Storage.from_url(f"sqlite:///file:{target}?uri=true")

        assert target.is_file()
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_memory_mode_file_uri_touches_no_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        ensure_sqlite_parent_dir(
            "sqlite:///file:ignored.db?mode=memory&cache=shared&uri=true"
        )

        assert list(tmp_path.iterdir()) == []

    def test_owner_only_modes_do_not_depend_on_process_umask(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        previous_umask = os.umask(0o022)
        try:
            Storage.from_url("sqlite:///secure/endure.db")
        finally:
            os.umask(previous_umask)

        assert stat.S_IMODE((tmp_path / "secure").stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / "secure" / "endure.db").stat().st_mode) == 0o600
