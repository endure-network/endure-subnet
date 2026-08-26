from pathlib import Path

import yaml

MINER_SERVICES = tuple(f"miner-{index}" for index in range(1, 6))
MINER_PORTS = tuple(range(8092, 8097))


def test_default_compose_selects_alpha_risk_for_every_service() -> None:
    root = Path(__file__).resolve().parents[1]
    default = (root / "docker-compose.yml").read_text()

    assert default.count('"--endure.active_schema", "risk.v1.subnet_alpha"') == 6
    assert default.count('"--endure.validator_axon_overrides"') == 5


def test_default_compose_isolates_wallets_and_keeps_read_api_local() -> None:
    root = Path(__file__).resolve().parents[1]
    services = yaml.safe_load((root / "docker-compose.yml").read_text())["services"]
    wallet_mounts = {
        service_name: next(
            str(volume)
            for volume in service["volumes"]
            if str(volume).endswith(":/root/.bittensor/wallets:ro")
        )
        for service_name, service in services.items()
    }

    assert len(set(wallet_mounts.values())) == len(wallet_mounts) == 6
    assert wallet_mounts["validator"].startswith("${VALIDATOR_WALLET_ROOT:")
    for index in range(1, 6):
        assert wallet_mounts[f"miner-{index}"].startswith(
            f"${{MINER{index}_WALLET_ROOT:"
        )
    assert "127.0.0.1:8714:8714" in services["validator"]["ports"]


def test_validator_container_liveness_does_not_use_degraded_readiness() -> None:
    root = Path(__file__).resolve().parents[1]
    compose_files = (
        root / "docker-compose.yml",
        root / "deploy/soak/docker-compose.yaml",
    )

    for compose_file in compose_files:
        compose = compose_file.read_text()
        assert "http://localhost:8714/live" in compose
        assert "http://localhost:8714/health" not in compose


def test_every_miner_compose_service_has_axon_healthcheck() -> None:
    root = Path(__file__).resolve().parents[1]
    compose_files = (
        root / "docker-compose.yml",
        root / "deploy/soak-miners/docker-compose.yaml",
    )

    for compose_file in compose_files:
        services = yaml.safe_load(compose_file.read_text())["services"]
        for service_name, port in zip(MINER_SERVICES, MINER_PORTS, strict=True):
            service = services[service_name]
            assert service["environment"]["MINER_AXON_PORT"] == str(port)
            healthcheck = service["healthcheck"]
            assert healthcheck["interval"] == "30s"
            assert healthcheck["timeout"] == "10s"
            assert healthcheck["retries"] == 3
            assert healthcheck["start_period"] == "180s"
            command = " ".join(healthcheck["test"])
            assert "socket.create_connection" in command
            assert "MINER_AXON_PORT" in command


def test_default_alpha_compose_configures_every_miner_healthcheck() -> None:
    root = Path(__file__).resolve().parents[1]
    services = yaml.safe_load((root / "docker-compose.yml").read_text())["services"]

    for service_name, port in zip(MINER_SERVICES, MINER_PORTS, strict=True):
        service = services[service_name]
        assert "socket.create_connection" in " ".join(service["healthcheck"]["test"])
        assert service["environment"]["MINER_AXON_PORT"] == str(port)


def test_soak_compose_keeps_named_data_volume_and_host_backed_snapshots() -> None:
    # Runtime data stays in a named volume while snapshots remain host-backed.
    compose = (
        Path(__file__).resolve().parents[1] / "deploy/soak/docker-compose.yaml"
    ).read_text()

    assert yaml.safe_load(compose) is not None
    assert "- validator-data:/data" in compose
    assert "\n  validator-data:\n" in compose
    assert (
        "- type: bind\n        source: /var/lib/endure-soak/backups\n        target: /data/backups"
        in compose
    )
