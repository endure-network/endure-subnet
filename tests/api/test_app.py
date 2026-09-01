"""Read API with embargo gate (spec §9)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from endure.api.app import PublicationIdentity, RuntimeHealth, build_app
from endure.assessment.coordinates import (
    AssessmentConsensusRow,
    AssessmentCoordinate,
    AssessmentEmaState,
    AssessmentRealizedTarget,
    AssessmentScoreHistoryRow,
)
from endure.assessment.registry import UniverseSnapshot
from endure.assessment.schemas.forge_lending import FORGE_LENDING_SCHEMA_ID
from endure.assessment.schemas.subnet_alpha_risk import (
    HORIZON_5D_SECONDS,
    HORIZON_30D_SECONDS,
    RISK_SCHEMA_ID,
    RiskOutput,
)
from endure.protocol.canonical import canonical_bundle_bytes
from endure.protocol.round_engine import DEFAULT_OFFSETS, compute_windows
from endure.runtime.identity import content_revision
from endure.scoring.assessment_orchestrator import REALIZED_TARGET_RESOLVED
from endure.storage.repository import Storage
from endure.storage.tables import rounds

ROUND = "2026-06-09"
NOW = datetime(2026, 6, 9, 21, 0, tzinfo=UTC).isoformat()


@pytest.fixture
def storage(migrated_storage: Storage) -> Storage:
    migrated_storage.open_round(
        windows=compute_windows(date(2026, 6, 9), offsets=DEFAULT_OFFSETS),
        schema_id=FORGE_LENDING_SCHEMA_ID,
        universe=UniverseSnapshot(round_id=ROUND, tickers=("44",), source_hash="h"),
        now_iso=NOW,
    )
    return migrated_storage


@pytest.fixture
def client(storage: Storage) -> TestClient:
    return TestClient(
        build_app(
            storage=storage, schema_id=FORGE_LENDING_SCHEMA_ID, publisher="assessment"
        )
    )


def _corrupt_round_state(storage: Storage, state: str) -> None:
    with storage._engine.begin() as connection:
        connection.execute(
            update(rounds)
            .where(
                rounds.c.round_id == ROUND,
                rounds.c.schema_id == FORGE_LENDING_SCHEMA_ID,
            )
            .values(state=state, updated_at=NOW)
        )


class TestHealthAndSchemas:
    def test_health(self, client: TestClient) -> None:
        response = client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["schema_id"] == FORGE_LENDING_SCHEMA_ID
        assert body["version"] == "0.1.0rc1"
        assert body["protocol_version_key"] == 29
        assert body["source_revision"] == "unknown"
        assert body["image_version"] == "dev"
        assert body["content_revision"] == content_revision()

    def test_schemas_discovery(self, client: TestClient) -> None:
        response = client.get("/schemas")

        assert response.status_code == 200
        schemas = {schema["schema_id"]: schema for schema in response.json()}
        assert set(schemas) == {FORGE_LENDING_SCHEMA_ID, RISK_SCHEMA_ID}
        assert (
            schemas[FORGE_LENDING_SCHEMA_ID]["serving_status"] == "registered_unserved"
        )
        assert schemas[RISK_SCHEMA_ID]["serving_status"] == "served"
        assert "collateral_factor" in schemas[FORGE_LENDING_SCHEMA_ID]["parameters"]
        assert "max_drawdown" in schemas[RISK_SCHEMA_ID]["parameters"]
        assert schemas[FORGE_LENDING_SCHEMA_ID]["horizons_seconds"] == [432000]
        assert schemas[RISK_SCHEMA_ID]["horizons_seconds"] == [432000, 2592000]
        assert all("horizons_trading_days" not in schema for schema in schemas.values())


def _runtime(
    *,
    tick_failures: int = 0,
    universe_failures: int = 0,
    resolution_failures: int = 0,
    empty_scored_rounds: int = 0,
    validator_loop_alive: bool = True,
    tick_stale: bool = False,
    seconds_since_last_tick: float | None = 1.5,
    set_weights_failures: int = 0,
    weight_emission_degraded: bool = False,
    rpc_degraded: bool = False,
    assessment_due_seconds: dict[int, int] | None = None,
) -> RuntimeHealth:
    runtime: RuntimeHealth = {
        "validator_loop_alive": validator_loop_alive,
        "tick_stale": tick_stale,
        "seconds_since_last_tick": seconds_since_last_tick,
        "consecutive_tick_failures": tick_failures,
        "last_tick_ok": NOW,
        "last_tick_error": None,
        "consecutive_universe_failures": universe_failures,
        "last_universe_error": None,
        "consecutive_resolution_failures": resolution_failures,
        "last_resolution_error": None,
        "consecutive_empty_scored_rounds": empty_scored_rounds,
        "last_empty_scored_round": None,
        "consecutive_set_weights_failures": set_weights_failures,
        "weight_emission_degraded": weight_emission_degraded,
        "failed_weight_submissions_total": 3,
        "rpc_gate": {
            "adaptive_rate": 1.0,
            "degraded": rpc_degraded,
            "rate_limited_total": 0,
            "deferred_total": 0,
        },
    }
    if assessment_due_seconds is not None:
        runtime["assessment_due_seconds"] = assessment_due_seconds
    return runtime


class TestRuntimeHealth:
    """/health must trip the HTTP STATUS (not just a body field) when the
    loop degrades — operators wire status-code monitors to it."""

    def _client(self, storage: Storage, runtime: RuntimeHealth) -> TestClient:
        return TestClient(
            build_app(
                storage=storage,
                schema_id=FORGE_LENDING_SCHEMA_ID,
                publisher="assessment",
                runtime_health=lambda: runtime,
            )
        )

    def test_healthy_loop_is_200_ok(self, storage: Storage) -> None:
        response = self._client(storage, _runtime()).get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["runtime"]["seconds_since_last_tick"] == 1.5
        assert response.json()["runtime"]["failed_weight_submissions_total"] == 3

    def test_tick_failures_degrade_to_503(self, storage: Storage) -> None:
        response = self._client(storage, _runtime(tick_failures=3)).get("/health")

        assert response.status_code == 503
        assert response.json()["status"] == "degraded"

    def test_dead_validator_loop_degrades_to_503(self, storage: Storage) -> None:
        response = self._client(storage, _runtime(validator_loop_alive=False)).get(
            "/health"
        )

        assert response.status_code == 503
        assert response.json()["status"] == "degraded"

    def test_legacy_external_runtime_shape_warns_and_degrades_without_server_error(
        self, storage: Storage, caplog: pytest.LogCaptureFixture
    ) -> None:
        runtime: RuntimeHealth = {
            "validator_loop_alive": False,
            "tick_stale": False,
            "seconds_since_last_tick": 1.5,
            "consecutive_tick_failures": 0,
            "last_tick_ok": NOW,
            "last_tick_error": None,
            "last_universe_error": None,
            "last_resolution_error": None,
            "last_empty_scored_round": None,
        }

        response = self._client(storage, runtime).get("/health")

        assert response.status_code == 503
        assert response.json()["status"] == "degraded"
        assert "runtime health payload omitted counters" in caplog.text

    def test_stale_validator_tick_degrades_to_503(self, storage: Storage) -> None:
        response = self._client(storage, _runtime(tick_stale=True)).get("/health")

        assert response.status_code == 503
        assert response.json()["status"] == "degraded"

    def test_rounds_not_opening_degrade_to_503(self, storage: Storage) -> None:
        response = self._client(storage, _runtime(universe_failures=2)).get("/health")

        assert response.status_code == 503
        assert response.json()["status"] == "degraded"

    def test_rounds_not_resolving_degrade_to_503(self, storage: Storage) -> None:
        response = self._client(storage, _runtime(resolution_failures=4)).get("/health")

        assert response.status_code == 503
        assert response.json()["status"] == "degraded"

    def test_active_rpc_backoff_degrades_to_503(self, storage: Storage) -> None:
        response = self._client(storage, _runtime(rpc_degraded=True)).get("/health")

        assert response.status_code == 503
        assert response.json()["status"] == "degraded"

    def test_live_endpoint_stays_200_during_rpc_backoff(self, storage: Storage) -> None:
        response = self._client(storage, _runtime(rpc_degraded=True)).get("/live")

        assert response.status_code == 200
        assert response.json() == {"status": "live"}

    def test_consecutive_weight_failures_degrade_to_503(self, storage: Storage) -> None:
        response = self._client(storage, _runtime(set_weights_failures=1)).get(
            "/health"
        )

        assert response.status_code == 503
        assert response.json()["status"] == "degraded"

    def test_overdue_weight_emission_degrades_to_503(self, storage: Storage) -> None:
        response = self._client(storage, _runtime(weight_emission_degraded=True)).get(
            "/health"
        )

        assert response.status_code == 503
        assert response.json()["status"] == "degraded"

    def test_single_empty_scored_round_stays_ok(self, storage: Storage) -> None:
        response = self._client(storage, _runtime(empty_scored_rounds=1)).get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_consecutive_empty_scored_rounds_degrade_to_503(
        self, storage: Storage
    ) -> None:
        response = self._client(storage, _runtime(empty_scored_rounds=2)).get("/health")

        assert response.status_code == 503
        assert response.json()["status"] == "degraded"


class TestRiskRoundResolutionHealth:
    def _open_round(self, storage: Storage) -> datetime:
        windows = compute_windows(date(2026, 6, 9), offsets=DEFAULT_OFFSETS)
        storage.open_round(
            windows=windows,
            schema_id=RISK_SCHEMA_ID,
            universe=UniverseSnapshot(
                round_id=ROUND, tickers=("8",), source_hash="risk-health"
            ),
            now_iso=NOW,
        )
        storage.set_round_state(ROUND, RISK_SCHEMA_ID, "revealed", now_iso=NOW)
        return windows.reveal_close

    def test_expected_horizon_backlog_stays_healthy(
        self, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reveal_close = self._open_round(storage)
        monkeypatch.setattr(
            "endure.api.app._utc_now", lambda: reveal_close + timedelta(days=4)
        )

        response = TestClient(
            build_app(storage=storage, schema_id=RISK_SCHEMA_ID, publisher="risk")
        ).get("/health")

        assert response.status_code == 200
        health = response.json()["round_resolution"]
        assert health["pending_round_count"] == 1
        assert health["overdue_round_count"] == 0
        [pending] = health["pending_rounds"]
        assert pending["round_id"] == ROUND
        assert [item["horizon_seconds"] for item in pending["pending_horizons"]] == [
            HORIZON_5D_SECONDS,
            HORIZON_30D_SECONDS,
        ]

    def test_missing_due_horizon_degrades_health(
        self, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reveal_close = self._open_round(storage)
        monkeypatch.setattr(
            "endure.api.app._utc_now", lambda: reveal_close + timedelta(days=6)
        )

        response = TestClient(
            build_app(storage=storage, schema_id=RISK_SCHEMA_ID, publisher="risk")
        ).get("/health")

        assert response.status_code == 503
        assert response.json()["status"] == "degraded"
        health = response.json()["round_resolution"]
        assert health["pending_round_count"] == 0
        assert health["overdue_round_count"] == 1
        [overdue] = health["overdue_rounds"]
        assert overdue["round_id"] == ROUND
        assert [item["horizon_seconds"] for item in overdue["overdue_horizons"]] == [
            HORIZON_5D_SECONDS
        ]

    def test_compressed_due_seconds_degrade_after_effective_deadline(
        self, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reveal_close = self._open_round(storage)
        monkeypatch.setattr(
            "endure.api.app._utc_now", lambda: reveal_close + timedelta(seconds=6)
        )
        runtime = _runtime(
            assessment_due_seconds={
                HORIZON_5D_SECONDS: 5,
                HORIZON_30D_SECONDS: 10,
            }
        )

        response = TestClient(
            build_app(
                storage=storage,
                schema_id=RISK_SCHEMA_ID,
                publisher="risk",
                runtime_health=lambda: runtime,
            )
        ).get("/health")

        assert response.status_code == 503
        [overdue] = response.json()["round_resolution"]["overdue_rounds"]
        assert [item["horizon_seconds"] for item in overdue["overdue_horizons"]] == [
            HORIZON_5D_SECONDS
        ]
        assert [item["horizon_seconds"] for item in overdue["pending_horizons"]] == [
            HORIZON_30D_SECONDS
        ]

    def test_exact_effective_deadline_remains_pending(
        self, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reveal_close = self._open_round(storage)
        monkeypatch.setattr(
            "endure.api.app._utc_now", lambda: reveal_close + timedelta(seconds=5)
        )
        runtime = _runtime(
            assessment_due_seconds={
                HORIZON_5D_SECONDS: 5,
                HORIZON_30D_SECONDS: 10,
            }
        )

        response = TestClient(
            build_app(
                storage=storage,
                schema_id=RISK_SCHEMA_ID,
                publisher="risk",
                runtime_health=lambda: runtime,
            )
        ).get("/health")

        assert response.status_code == 200
        assert response.json()["round_resolution"]["overdue_round_count"] == 0

    def test_completed_short_horizon_reports_only_long_horizon_pending(
        self, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reveal_close = self._open_round(storage)
        storage.record_assessment_scoring_pass(
            ROUND,
            RISK_SCHEMA_ID,
            horizon_value=HORIZON_5D_SECONDS,
            realized_targets=(),
            output_scores=(),
            ema_updates=(),
            score_history=(),
            now_iso=(reveal_close + timedelta(days=5, seconds=1)).isoformat(),
        )
        monkeypatch.setattr(
            "endure.api.app._utc_now", lambda: reveal_close + timedelta(days=6)
        )

        response = TestClient(
            build_app(storage=storage, schema_id=RISK_SCHEMA_ID, publisher="risk")
        ).get("/health")

        assert response.status_code == 200
        health = response.json()["round_resolution"]
        [pending] = health["pending_rounds"]
        assert [item["horizon_seconds"] for item in pending["pending_horizons"]] == [
            HORIZON_30D_SECONDS
        ]


class TestEmbargo:
    def test_open_round_consensus_is_embargoed(self, client: TestClient) -> None:
        assert client.get(f"/rounds/{ROUND}/consensus").status_code == 403

    def test_open_round_submissions_are_embargoed(self, client: TestClient) -> None:
        assert client.get(f"/rounds/{ROUND}/submissions").status_code == 403

    def test_open_round_outcomes_are_embargoed(self, client: TestClient) -> None:
        assert client.get(f"/rounds/{ROUND}/outcomes").status_code == 403

    @pytest.mark.parametrize(
        "path",
        [
            f"/rounds/{ROUND}/consensus",
            f"/rounds/{ROUND}/scores",
            f"/rounds/{ROUND}/submissions",
            f"/rounds/{ROUND}/outcomes",
        ],
    )
    def test_invalid_round_state_is_embargoed(
        self, storage: Storage, client: TestClient, path: str
    ) -> None:
        _corrupt_round_state(storage, "opne")

        assert client.get(path).status_code == 403

    def test_unknown_round_is_404(self, client: TestClient) -> None:
        assert client.get("/rounds/1999-01-01/consensus").status_code == 404

    def test_round_meta_is_always_readable(self, client: TestClient) -> None:
        response = client.get(f"/rounds/{ROUND}")

        assert response.status_code == 200
        assert response.json()["state"] == "open"

    def test_round_meta_hides_accepted_participation_while_open(
        self, storage: Storage, client: TestClient
    ) -> None:
        storage.record_reveal(
            ROUND,
            FORGE_LENDING_SCHEMA_ID,
            "hk-a",
            bundle_json='{"accepted":true}',
            nonce_hex="01",
            accepted=True,
            rejection_code=None,
            now_iso=NOW,
        )

        response = client.get(f"/rounds/{ROUND}")

        assert response.status_code == 200
        assert response.json()["accepted_submissions"] is None
        assert client.get(f"/rounds/{ROUND}/submissions").status_code == 403

    def test_round_meta_exposes_participation_only_after_strict_boundary(
        self,
        storage: Storage,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        storage.record_reveal(
            ROUND,
            FORGE_LENDING_SCHEMA_ID,
            "hk-a",
            bundle_json='{"accepted":true}',
            nonce_hex="01",
            accepted=True,
            rejection_code=None,
            now_iso=NOW,
        )
        storage.set_round_state(ROUND, FORGE_LENDING_SCHEMA_ID, "revealed", now_iso=NOW)
        meta = storage.round_meta(ROUND, FORGE_LENDING_SCHEMA_ID)
        assert meta is not None
        publication_available_at = datetime.fromisoformat(
            str(meta["publication_available_at"])
        )

        monkeypatch.setattr("endure.api.app._utc_now", lambda: publication_available_at)
        assert client.get(f"/rounds/{ROUND}").json()["accepted_submissions"] is None

        monkeypatch.setattr(
            "endure.api.app._utc_now",
            lambda: publication_available_at + timedelta(microseconds=1),
        )
        assert client.get(f"/rounds/{ROUND}").json()["accepted_submissions"] == 1

    def test_universe_is_served_while_round_is_open(self, client: TestClient) -> None:
        """Miners need the frozen universe DURING the commit window — the
        universe is round input, not submission data, so it is not
        embargoed."""
        response = client.get(f"/rounds/{ROUND}/universe")

        assert response.status_code == 200
        body = response.json()
        assert body["round_id"] == ROUND
        assert body["tickers"] == ["44"]
        assert body["source_hash"] == "h"

    def test_universe_unknown_round_is_404(self, client: TestClient) -> None:
        assert client.get("/rounds/1999-01-01/universe").status_code == 404


class TestAfterEmbargo:
    def test_outcomes_served_after_reveal(
        self, storage: Storage, client: TestClient
    ) -> None:
        storage.set_round_state(ROUND, FORGE_LENDING_SCHEMA_ID, "revealed", now_iso=NOW)

        response = client.get(f"/rounds/{ROUND}/outcomes")

        assert response.status_code == 200
        assert response.json() == []

    def test_unknown_miner_is_404(self, client: TestClient) -> None:
        assert client.get("/miners/hk-nobody/scores").status_code == 404

    def test_revealed_round_stays_embargoed_through_publication_boundary(
        self, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        windows = compute_windows(date(2026, 6, 9), offsets=DEFAULT_OFFSETS)
        publication_available_at = windows.commit_close + timedelta(days=1)
        storage.open_round(
            windows=windows,
            schema_id=RISK_SCHEMA_ID,
            universe=UniverseSnapshot(
                round_id=ROUND, tickers=("8",), source_hash="embargo"
            ),
            now_iso=NOW,
            publication_available_at=publication_available_at,
        )
        storage.set_round_state(ROUND, RISK_SCHEMA_ID, "revealed", now_iso=NOW)
        risk_client = TestClient(
            build_app(storage=storage, schema_id=RISK_SCHEMA_ID, publisher="risk")
        )
        monkeypatch.setattr("endure.api.app._utc_now", lambda: publication_available_at)

        assert risk_client.get(f"/rounds/{ROUND}/consensus").status_code == 403

        monkeypatch.setattr(
            "endure.api.app._utc_now",
            lambda: publication_available_at + timedelta(microseconds=1),
        )
        assert risk_client.get(f"/rounds/{ROUND}/consensus").status_code == 200


class TestRiskFeed:
    def _risk_client(self, storage: Storage) -> TestClient:
        return TestClient(
            build_app(
                storage=storage,
                schema_id=RISK_SCHEMA_ID,
                publisher="risk",
                publication_identity=PublicationIdentity(
                    signer=lambda payload: b"risk:" + payload[:3],
                    hotkey="validator-hotkey",
                ),
            )
        )

    def _open_risk_round(self, storage: Storage) -> None:
        storage.open_round(
            windows=compute_windows(date(2026, 6, 9), offsets=DEFAULT_OFFSETS),
            schema_id=RISK_SCHEMA_ID,
            universe=UniverseSnapshot(
                round_id=ROUND, tickers=("8", "44"), source_hash="h"
            ),
            now_iso=NOW,
        )

    def _consensus_row(
        self, netuid: int, output: RiskOutput, value: int
    ) -> AssessmentConsensusRow:
        return AssessmentConsensusRow(
            coordinate=AssessmentCoordinate.subnet_asset(
                netuid=netuid,
                horizon_seconds=HORIZON_30D_SECONDS,
                output=output.value,
            ),
            value=Decimal(value),
            dispersion=Decimal(25),
            n_submitters=4,
        )

    def _resolved_target(
        self, netuid: int, output: RiskOutput
    ) -> AssessmentRealizedTarget:
        return AssessmentRealizedTarget(
            coordinate=AssessmentCoordinate.subnet_asset(
                netuid=netuid,
                horizon_seconds=HORIZON_30D_SECONDS,
                output=output.value,
            ),
            value=Decimal(1),
            status=REALIZED_TARGET_RESOLVED,
        )

    def test_risk_feed_serves_signed_consensus_payload(self, storage: Storage) -> None:
        self._open_risk_round(storage)
        storage.publish_assessment_consensus_and_reveal(
            ROUND,
            RISK_SCHEMA_ID,
            [
                self._consensus_row(8, RiskOutput.MAX_DRAWDOWN, 1999),
                self._consensus_row(8, RiskOutput.REALIZED_VOLATILITY, 7999),
            ],
            now_iso=NOW,
        )
        storage.record_assessment_realized_targets(
            ROUND,
            RISK_SCHEMA_ID,
            [
                self._resolved_target(8, RiskOutput.MAX_DRAWDOWN),
                self._resolved_target(8, RiskOutput.REALIZED_VOLATILITY),
            ],
            now_iso=NOW,
        )

        response = self._risk_client(storage).get("/risk/v1/subnets")

        assert response.status_code == 200
        body = response.json()
        assert body["signature"]["signed_by"] == "validator-hotkey"
        assert body["signature"]["signature_hex"] == "7269736b3a7b2261"
        assert body["signature"]["canonical_payload_sha256"]
        payload = body["payload"]
        assert canonical_bundle_bytes(payload).startswith(b'{"as_of"')
        assert payload["feed_schema_version"] == 1
        assert payload["schema_id"] == RISK_SCHEMA_ID
        assert payload["round_id"] == ROUND
        assert "tier_round_id" not in payload
        assert "tier_as_of" not in payload
        subnet_by_netuid = {subnet["netuid"]: subnet for subnet in payload["subnets"]}
        assert subnet_by_netuid[8]["tier"] == "B"
        assert subnet_by_netuid[8]["tier_round_id"] == ROUND
        assert subnet_by_netuid[8]["tier_as_of"] is not None
        assert subnet_by_netuid[44]["tier"] == "unrated"
        assert subnet_by_netuid[44]["tier_round_id"] is None
        assert subnet_by_netuid[44]["tier_as_of"] is None
        [drawdown, volatility] = subnet_by_netuid[8]["consensus"]
        assert drawdown["median"] == "1999"
        assert drawdown["mad"] == "25"
        assert volatility["n_submitters"] == 4

    def test_risk_feed_empty_consensus_is_unrated(self, storage: Storage) -> None:
        response = self._risk_client(storage).get("/risk/v1/subnets")

        assert response.status_code == 200
        payload = response.json()["payload"]
        assert payload["round_id"] is None
        assert payload["feed_schema_version"] == 1
        assert "tier_round_id" not in payload
        assert "tier_as_of" not in payload
        assert {subnet["tier"] for subnet in payload["subnets"]} == {"unrated"}
        assert {subnet["tier_round_id"] for subnet in payload["subnets"]} == {None}
        assert {subnet["tier_as_of"] for subnet in payload["subnets"]} == {None}


class TestRiskAssessmentReadRoutes:
    def _client(self, storage: Storage) -> TestClient:
        return TestClient(
            build_app(storage=storage, schema_id=RISK_SCHEMA_ID, publisher="risk")
        )

    def _coordinate(self, netuid: int, output: RiskOutput) -> AssessmentCoordinate:
        return AssessmentCoordinate.subnet_asset(
            netuid=netuid,
            horizon_seconds=HORIZON_30D_SECONDS,
            output=output.value,
        )

    def _open_round(self, storage: Storage) -> None:
        storage.open_round(
            windows=compute_windows(date(2026, 6, 9), offsets=DEFAULT_OFFSETS),
            schema_id=RISK_SCHEMA_ID,
            universe=UniverseSnapshot(
                round_id=ROUND, tickers=("8", "44"), source_hash="risk-h"
            ),
            now_iso=NOW,
        )

    def _seed_assessment_rows(self, storage: Storage) -> None:
        self._open_round(storage)
        coordinate = self._coordinate(8, RiskOutput.MAX_DRAWDOWN)
        storage.publish_assessment_consensus_and_reveal(
            ROUND,
            RISK_SCHEMA_ID,
            [
                AssessmentConsensusRow(
                    coordinate=coordinate,
                    value=Decimal(1500),
                    dispersion=Decimal(33),
                    n_submitters=5,
                )
            ],
            now_iso=NOW,
        )
        storage.record_assessment_scoring_pass(
            ROUND,
            RISK_SCHEMA_ID,
            horizon_value=HORIZON_30D_SECONDS,
            realized_targets=[
                AssessmentRealizedTarget(
                    coordinate=coordinate,
                    value=Decimal(1400),
                    status=REALIZED_TARGET_RESOLVED,
                )
            ],
            output_scores=[],
            ema_updates=[
                AssessmentEmaState(
                    miner_hotkey="hk-good",
                    coordinate=coordinate,
                    ema=Decimal("0.9"),
                    resolved_rounds=2,
                ),
                AssessmentEmaState(
                    miner_hotkey="hk-weak",
                    coordinate=coordinate,
                    ema=Decimal("0.3"),
                    resolved_rounds=1,
                ),
            ],
            score_history=[
                AssessmentScoreHistoryRow(
                    miner_hotkey="hk-good",
                    coordinate=coordinate,
                    round_score=Decimal("0.8"),
                    ema_after=Decimal("0.9"),
                )
            ],
            now_iso=NOW,
        )

    def test_miners_leaderboard_uses_assessment_emas(self, storage: Storage) -> None:
        self._seed_assessment_rows(storage)

        rows = self._client(storage).get("/miners").json()

        assert [row["miner_hotkey"] for row in rows] == ["hk-good", "hk-weak"]
        assert rows[0]["blended_score"] == "0.9"
        assert rows[0]["horizon_emas"] == {"2592000": "0.9"}
        assert Decimal(rows[0]["weight_share"]) > Decimal(rows[1]["weight_share"])

    def test_round_data_routes_use_assessment_tables(self, storage: Storage) -> None:
        self._seed_assessment_rows(storage)
        client = self._client(storage)

        [score] = client.get(f"/rounds/{ROUND}/scores").json()
        assert score["miner_hotkey"] == "hk-good"
        assert score["target_id"] == "8"
        assert score["horizon_seconds"] == HORIZON_30D_SECONDS
        assert score["round_score"] == "0.8"

        [consensus] = client.get(f"/rounds/{ROUND}/consensus").json()
        assert consensus["target_id"] == "8"
        assert consensus["output"] == RiskOutput.MAX_DRAWDOWN.value
        assert consensus["median"] == "1500"
        assert consensus["mad"] == "33"

        [outcome] = client.get(f"/rounds/{ROUND}/outcomes").json()
        assert outcome["target_id"] == "8"
        assert outcome["horizon_seconds"] == HORIZON_30D_SECONDS
        assert outcome["value"] == "1400"
        assert outcome["status"] == REALIZED_TARGET_RESOLVED

    def test_assessment_miner_scores_and_history_policy(self, storage: Storage) -> None:
        self._seed_assessment_rows(storage)
        client = self._client(storage)

        scores = client.get("/miners/hk-good/scores").json()
        assert scores["miner_hotkey"] == "hk-good"
        assert scores["emas"] == [
            {
                "target_kind": "subnet_asset",
                "target_id": "8",
                "horizon_seconds": HORIZON_30D_SECONDS,
                "output": RiskOutput.MAX_DRAWDOWN.value,
                "ema": "0.9",
                "resolved_rounds": 2,
            }
        ]


class TestDashboardSurface:
    """Enumeration endpoints and CORS so browser dashboards can exist at
    all."""

    def test_rounds_index_lists_rounds_with_state(self, client: TestClient) -> None:
        response = client.get("/rounds")

        assert response.status_code == 200
        [row] = response.json()
        assert row["round_id"] == ROUND
        assert row["state"] == "open"

    def test_rounds_index_filters_by_state(self, client: TestClient) -> None:
        assert client.get("/rounds?state=closed").json() == []

    def test_rounds_index_before_cursor_pages_older_rounds(
        self, storage: Storage, client: TestClient
    ) -> None:
        storage.open_round(
            windows=compute_windows(date(2026, 6, 8), offsets=DEFAULT_OFFSETS),
            schema_id=FORGE_LENDING_SCHEMA_ID,
            universe=UniverseSnapshot(
                round_id="2026-06-08", tickers=("WAL",), source_hash="h"
            ),
            now_iso=NOW,
        )

        rows = client.get("/rounds?before=2026-06-09").json()

        assert [row["round_id"] for row in rows] == ["2026-06-08"]

    def test_cors_headers_allow_browser_dashboards(self, client: TestClient) -> None:
        response = client.get("/health", headers={"Origin": "https://example.org"})

        assert response.headers.get("access-control-allow-origin") == "*"


class TestPublicApiHardening:
    """Bounded, paginated read endpoints + a CORS preflight that succeeds."""

    def _seed_submissions(self, storage: Storage, count: int) -> None:
        for index in range(count):
            hotkey = f"hk-{index:02d}"
            storage.record_commit(
                ROUND, FORGE_LENDING_SCHEMA_ID, hotkey, "ab" * 32, now_iso=NOW
            )
            storage.record_reveal(
                ROUND,
                FORGE_LENDING_SCHEMA_ID,
                hotkey,
                bundle_json=f'{{"i":{index}}}',
                nonce_hex="0102",
                accepted=True,
                rejection_code=None,
                now_iso=NOW,
            )
        storage.set_round_state(ROUND, FORGE_LENDING_SCHEMA_ID, "revealed", now_iso=NOW)

    def test_health_reports_count_and_caps_unfinished_rounds(
        self, client: TestClient
    ) -> None:
        body = client.get("/health").json()

        assert body["unfinished_round_count"] >= 1
        assert isinstance(body["unfinished_rounds"], list)
        assert len(body["unfinished_rounds"]) <= 10

    def test_submissions_are_paginated_and_report_has_more(
        self, storage: Storage, client: TestClient
    ) -> None:
        self._seed_submissions(storage, 5)

        page1 = client.get(f"/rounds/{ROUND}/submissions?limit=2").json()
        assert page1["limit"] == 2
        assert page1["has_more"] is True
        assert [s["miner_hotkey"] for s in page1["submissions"]] == ["hk-00", "hk-01"]

        page3 = client.get(f"/rounds/{ROUND}/submissions?limit=2&offset=4").json()
        assert page3["offset"] == 4
        assert page3["has_more"] is False
        assert [s["miner_hotkey"] for s in page3["submissions"]] == ["hk-04"]

    def test_cors_preflight_allows_custom_request_headers(
        self, client: TestClient
    ) -> None:
        response = client.options(
            "/health",
            headers={
                "Origin": "https://example.org",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-custom",
            },
        )

        assert response.status_code in (200, 204)
        assert response.headers.get("access-control-allow-origin") == "*"
