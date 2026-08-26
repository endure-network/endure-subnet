import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_operator_deploy_rejects_equal_and_overlapping_wallet_roots(
    tmp_path: Path,
) -> None:
    wallet_root = tmp_path / "wallets"
    nested_wallet_root = wallet_root / "miner"
    nested_wallet_root.mkdir(parents=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
if [[ "$*" == *"config --format json"* ]]; then
  printf '{"services":{"validator":{"volumes":[{"source":"%s","target":"/root/.bittensor/wallets"}]},"miner-1":{"volumes":[{"source":"%s","target":"/root/.bittensor/wallets"}]}}}' "$TEST_VALIDATOR_WALLET" "$TEST_MINER_WALLET"
fi
"""
    )
    fake_docker.chmod(0o755)
    env_file = tmp_path / "operator.env"
    env_file.write_text("")

    for miner_root in (wallet_root, nested_wallet_root):
        result = subprocess.run(
            ["bash", str(ROOT / "deploy/operator-node/deploy.sh")],
            check=False,
            capture_output=True,
            text=True,
            env={
                "ENDURE_ENV_FILE": str(env_file),
                "PATH": f"{fake_bin}:{Path(sys.executable).parent}:/usr/bin:/bin",
                "TEST_VALIDATOR_WALLET": str(wallet_root),
                "TEST_MINER_WALLET": str(miner_root),
            },
        )

        assert result.returncode != 0
        assert "separate, non-overlapping directories" in result.stderr


def test_rollback_stops_partial_restart_after_start_or_health_failure(
    tmp_path: Path,
) -> None:
    deploy_script = (ROOT / "deploy/operator-node/deploy.sh").read_text()
    function_start = deploy_script.index("rollback_failed_release() {")
    function_end = deploy_script.index(
        '\n}\n\nif ! "${compose[@]}" up -d', function_start
    )
    rollback_function = deploy_script[function_start : function_end + 2]
    harness = tmp_path / "rollback-harness.sh"
    event_log = tmp_path / "events.log"
    record_dir = tmp_path / "release"
    record_dir.mkdir()
    (record_dir / "backup.sha256").write_text("")
    backup_file = tmp_path / "backup.db"
    backup_file.write_text("")
    harness.write_text(
        rollback_function
        + """
compose=(fake_compose)
previous_validator_image=validator@sha256:old
previous_miner_image=miner@sha256:old
current_validator_id=validator-id
backup_file="$TEST_BACKUP_FILE"
record_dir="$TEST_RECORD_DIR"
timestamp=test
restore_program=restore
fake_compose() {
  if [[ " $* " == *" stop "* ]]; then
    printf 'stop\\n' >>"$TEST_EVENT_LOG"
  elif [[ " $* " == *" ps -aq validator "* ]]; then
    printf 'validator-id\\n'
  elif [[ " $* " == *" up "* ]]; then
    printf 'up\\n' >>"$TEST_EVENT_LOG"
    [[ "${TEST_UP_FAIL:-0}" != "1" ]]
  fi
}
docker() {
  if [[ "$1" == "inspect" ]]; then
    printf 'image-id\\n'
  fi
}
sha256sum() { return 0; }
wait_for_healthy() { [[ "${TEST_HEALTH_FAIL:-}" != "$1" ]]; }
if rollback_failed_release; then
  exit 99
fi
"""
    )

    for scenario in ({"TEST_UP_FAIL": "1"}, {"TEST_HEALTH_FAIL": "validator"}):
        event_log.write_text("")
        result = subprocess.run(
            ["bash", str(harness)],
            check=False,
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "TEST_BACKUP_FILE": str(backup_file),
                "TEST_EVENT_LOG": str(event_log),
                "TEST_RECORD_DIR": str(record_dir),
                **scenario,
            },
        )

        assert result.returncode == 0
        assert event_log.read_text().splitlines().count("stop") == 2


def test_runtime_images_embed_oci_source_identity() -> None:
    for dockerfile_name in ("validator.Dockerfile", "miner.Dockerfile"):
        dockerfile = (ROOT / "docker" / dockerfile_name).read_text()

        assert "ARG ENDURE_SOURCE_REVISION" in dockerfile
        assert "ARG ENDURE_SOURCE_URL" in dockerfile
        assert "ARG ENDURE_IMAGE_VERSION" in dockerfile
        assert "org.opencontainers.image.revision=$ENDURE_SOURCE_REVISION" in dockerfile
        assert "org.opencontainers.image.source=$ENDURE_SOURCE_URL" in dockerfile
        assert "org.opencontainers.image.version=$ENDURE_IMAGE_VERSION" in dockerfile


def test_release_workflow_publishes_only_a_green_staging_sha() -> None:
    workflow = (ROOT / ".github/workflows/publish-release-images.yml").read_text()
    workflow_config = yaml.safe_load(workflow)
    triggers = workflow_config[True]

    assert triggers["push"]["branches"] == ["staging"]
    assert "workflow_dispatch" in triggers
    assert "SOURCE_SHA: ${{ inputs.source_sha || github.sha }}" in workflow
    assert "source_sha:" in workflow
    assert "packages: write" in workflow
    assert "actions: read" in workflow
    assert "repos/$GITHUB_REPOSITORY/git/ref/heads/staging" in workflow
    assert 'test "$staging_sha" = "$SOURCE_SHA"' in workflow
    assert workflow.count('test "$staging_sha" = "$SOURCE_SHA"') == 3
    assert "for _ in {1..360}; do" in workflow
    assert "sleep 10" in workflow
    assert (
        workflow.index("      - name: Wait for successful release workflows")
        < workflow.index("      - name: Recheck the current staging commit")
        < workflow.index("      - name: Authenticate to the container registry")
        < workflow.index("      - name: Build validator")
        < workflow.index("      - name: Build miner")
        < workflow.index("      - name: Recheck release qualification")
        < workflow.index("      - name: Publish images")
        < workflow.index("      - name: Record deployable digests")
    )
    assert "git fetch" not in workflow
    assert workflow.count("scripts/quality_gates/require_release_workflows.sh") == 2
    assert "commits/$SOURCE_SHA/check-runs" not in workflow
    assert '--build-arg ENDURE_SOURCE_REVISION="$SOURCE_SHA"' in workflow
    assert workflow.count("docker push") == 2
    assert "@sha256:" in workflow
    action_references = re.findall(
        r"^\s*- uses: ([^\s#]+)", workflow, flags=re.MULTILINE
    )
    assert action_references
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref) for ref in action_references)
    assert "persist-credentials: false" in workflow

    # `(?:- )?` also matches bare `uses:` entries under `- name:` steps.
    workflow_paths = sorted((ROOT / ".github/workflows").glob("*.yml"))
    assert workflow_paths
    seen_references = 0
    for workflow_path in workflow_paths:
        references = re.findall(
            r"^\s*(?:- )?uses: ([^\s#]+)",
            workflow_path.read_text(),
            flags=re.MULTILINE,
        )
        seen_references += len(references)
        unpinned = [
            ref for ref in references if not re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref)
        ]
        assert not unpinned, f"{workflow_path.name}: {unpinned}"
    assert seen_references


def test_operator_compose_uses_only_pinned_images_and_host_durability() -> None:
    compose_path = ROOT / "deploy/operator-node/docker-compose.yaml"
    compose_text = compose_path.read_text()
    services = yaml.safe_load(compose_text)["services"]

    validator = services["validator"]
    miner = services["miner-1"]
    assert "build" not in validator
    assert "build" not in miner
    assert validator["image"].startswith("${VALIDATOR_IMAGE:")
    assert miner["image"].startswith("${MINER_IMAGE:")
    assert "validator-data:/data" in validator["volumes"]
    assert all(
        "/var/lib/endure-node/backups" not in str(volume)
        for volume in validator["volumes"]
    )
    assert "miner-1-state:/root/.bittensor/miners" in miner["volumes"]
    validator_wallet = next(
        str(volume)
        for volume in validator["volumes"]
        if str(volume).endswith(":/root/.bittensor/wallets:ro")
    )
    miner_wallet = next(
        str(volume)
        for volume in miner["volumes"]
        if str(volume).endswith(":/root/.bittensor/wallets:ro")
    )
    assert validator_wallet.startswith("${VALIDATOR_WALLET_ROOT:")
    assert miner_wallet.startswith("${MINER_WALLET_ROOT:")
    assert validator_wallet != miner_wallet
    assert "http://localhost:8714/live" in " ".join(validator["healthcheck"]["test"])
    ci_workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "deploy/operator-node/env.example" in ci_workflow
    assert "deploy/operator-node/docker-compose.yaml config" in ci_workflow


def test_operator_deploy_rejects_mutable_images_and_records_rollback() -> None:
    deploy_script = (ROOT / "deploy/operator-node/deploy.sh").read_text()

    assert "@sha256:[0-9a-f]{64}" in deploy_script
    assert "previous-images.txt" in deploy_script
    assert "sqlite3.connect" in deploy_script
    assert "PRAGMA integrity_check" in deploy_script
    assert (
        "Refusing mainnet until the repository mainnet gate is lifted." in deploy_script
    )
    assert "ps -aq validator" in deploy_script
    assert 'state_volume="endure-subnet_validator-data"' in deploy_script
    assert 'docker volume inspect "$state_volume"' in deploy_script
    assert (
        "Existing validator state cannot be backed up without its container."
        in deploy_script
    )
    assert "rollback_failed_release" in deploy_script
    for guarded_command in (
        'sha256sum --check "$record_dir/backup.sha256" || return 1',
        'docker cp "$backup_file" "$current_validator_id:$restore_inside" || return 1',
        '"$current_validator_image" -c "$restore_program" || return 1',
    ):
        assert guarded_command in deploy_script
    assert "--pull never" in deploy_script
    assert "realpath" in deploy_script
    assert "separate, non-overlapping directories" in deploy_script
    assert "--mount" not in deploy_script
    assert (
        'if ! "${compose[@]}" up -d --no-build validator miner-1; then' in deploy_script
    )
    assert deploy_script.count("--entrypoint python") == 3
    assert "rendered-compose.yaml" not in deploy_script
    assert "docker compose" in deploy_script
    assert "http://127.0.0.1:8714/live" in deploy_script
    assert "http://127.0.0.1:8714/health" in deploy_script
