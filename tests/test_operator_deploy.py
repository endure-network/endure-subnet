import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
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
previous_validator_image_id=sha256:old-validator
previous_miner_image_id=sha256:old-miner
release_identity="sha256:new-validator sha256:new-miner"
rejected_file="$TEST_RECORD_DIR/rejected-releases.txt"
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
    printf 'up %s %s %s\\n' "$VALIDATOR_IMAGE" "$MINER_IMAGE" "$*" >>"$TEST_EVENT_LOG"
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
        events = event_log.read_text().splitlines()
        assert events.count("stop") == 2
        # A mutable tag already points at the failed release, so the previous
        # release is only reachable through its recorded local image ID.
        restarts = [event for event in events if event.startswith("up ")]
        assert len(restarts) == 1
        assert restarts[0].startswith("up sha256:old-validator sha256:old-miner ")
        assert "--pull never" in restarts[0]


def test_runtime_images_embed_oci_source_identity() -> None:
    for dockerfile_name in ("validator.Dockerfile", "miner.Dockerfile"):
        dockerfile = (ROOT / "docker" / dockerfile_name).read_text()

        assert "ARG ENDURE_SOURCE_REVISION" in dockerfile
        assert "ARG ENDURE_SOURCE_URL" in dockerfile
        assert "ARG ENDURE_IMAGE_VERSION" in dockerfile
        assert "ENV ENDURE_SOURCE_REVISION=$ENDURE_SOURCE_REVISION" in dockerfile
        assert "ENDURE_IMAGE_VERSION=$ENDURE_IMAGE_VERSION" in dockerfile
        assert "org.opencontainers.image.revision=$ENDURE_SOURCE_REVISION" in dockerfile
        assert "org.opencontainers.image.source=$ENDURE_SOURCE_URL" in dockerfile
        assert "org.opencontainers.image.version=$ENDURE_IMAGE_VERSION" in dockerfile
        # Without PYTHONPATH=/app the entrypoint imports `endure` from
        # site-packages, where no `neurons/` sits beside it, and
        # content_revision fails at boot instead of attesting the sources.
        assert "PYTHONPATH=/app" in dockerfile


def test_runtime_images_validate_release_identity_before_dependencies() -> None:
    for dockerfile_name in ("validator.Dockerfile", "miner.Dockerfile"):
        dockerfile = (ROOT / "docker" / dockerfile_name).read_text()

        check_at = dockerfile.index("RUN sh check-release-identity.sh")
        assert check_at < dockerfile.index("uv export --locked")
        assert "COPY docker/check-release-identity.sh" in dockerfile


def _release_identity_check(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(ROOT / "docker" / "check-release-identity.sh")],
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_release_identity_check_passes_dev_builds_without_args() -> None:
    assert _release_identity_check({}).returncode == 0
    assert _release_identity_check({"ENDURE_IMAGE_VERSION": "dev"}).returncode == 0


def test_release_identity_check_accepts_a_full_matching_revision() -> None:
    sha = "ab" * 20
    result = _release_identity_check(
        {"ENDURE_SOURCE_REVISION": sha, "ENDURE_IMAGE_VERSION": f"sha-{sha}"}
    )

    assert result.returncode == 0


def test_release_identity_check_refuses_an_abbreviated_revision() -> None:
    result = _release_identity_check(
        {"ENDURE_SOURCE_REVISION": "abcdef1", "ENDURE_IMAGE_VERSION": "sha-abcdef1"}
    )

    assert result.returncode == 1
    assert "full 40-hex commit" in result.stderr


def test_release_identity_check_refuses_partial_dev_metadata() -> None:
    for revision in ("abcdef1", "ab" * 20):
        result = _release_identity_check({"ENDURE_SOURCE_REVISION": revision})

        assert result.returncode == 1
        assert "requires the matching ENDURE_IMAGE_VERSION" in result.stderr


def test_release_identity_check_refuses_a_mismatched_version() -> None:
    sha = "ab" * 20
    result = _release_identity_check(
        {"ENDURE_SOURCE_REVISION": sha, "ENDURE_IMAGE_VERSION": "sha-" + "cd" * 20}
    )

    assert result.returncode == 1
    assert f"must be sha-{sha}" in result.stderr


def test_coolify_soak_compose_passes_exact_identity_as_build_args() -> None:
    expected = {
        "ENDURE_SOURCE_REVISION": "${SOURCE_COMMIT:-unknown}",
        "ENDURE_IMAGE_VERSION": "sha-${SOURCE_COMMIT:-unknown}",
    }
    for relative_path, service in (
        ("deploy/soak/docker-compose.yaml", "validator"),
        ("deploy/soak-miners/docker-compose.yaml", "miner-1"),
    ):
        compose = yaml.safe_load((ROOT / relative_path).read_text())

        assert compose["services"][service]["build"]["args"] == expected


def test_coolify_wallet_initializers_use_the_pinned_runtime_base() -> None:
    expected = (
        "python:3.12-slim@sha256:"
        "57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
    )
    for relative_path in (
        "deploy/soak/docker-compose.yaml",
        "deploy/soak-miners/docker-compose.yaml",
    ):
        compose = yaml.safe_load((ROOT / relative_path).read_text())
        wallet_init = compose["services"]["wallet-init"]

        assert wallet_init["image"] == expected
        assert "WALLETS_TAR_B64" in wallet_init["environment"]
        assert "wallets:/wallets" in wallet_init["volumes"]


def test_soak_probe_requires_readiness_and_exact_release_identity() -> None:
    workflow = (ROOT / ".github/workflows/soak-health-probe.yml").read_text()

    assert "https://api.testnet.endure.network/health" in workflow
    assert "https://api.testnet.endure.network/live" not in workflow
    assert "vars.SOAK_EXPECTED_SHA" in workflow
    assert "vars.SOAK_EXPECTED_PROTOCOL_KEY" in workflow
    assert "SOAK_EXPECTED_PROTOCOL_KEY:?" in workflow
    assert ":-29" not in workflow
    assert '.status == "ok"' in workflow
    assert '.schema_id == "risk.v1.subnet_alpha"' in workflow
    assert ".protocol_version_key == $key" in workflow
    assert ".source_revision == $sha" in workflow
    assert "(.runtime.process_uptime_seconds >= $min_uptime)" in workflow
    assert 'SOAK_MIN_UPTIME_SECONDS: "600"' in workflow


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
    assert "ghcr.io/$owner/endure-subnet-validator:sha-$SOURCE_SHA" in workflow
    assert "ghcr.io/$owner/endure-subnet-miner:sha-$SOURCE_SHA" in workflow
    assert "ghcr.io/$owner/endure-validator:sha-$SOURCE_SHA" not in workflow
    assert "ghcr.io/$owner/endure-miner:sha-$SOURCE_SHA" not in workflow
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


def test_operator_compose_follows_the_prod_channel_with_host_durability() -> None:
    compose_path = ROOT / "deploy/operator-node/docker-compose.yaml"
    compose_text = compose_path.read_text()
    services = yaml.safe_load(compose_text)["services"]

    validator = services["validator"]
    miner = services["miner-1"]
    assert "build" not in validator
    assert "build" not in miner
    assert validator["image"] == (
        "${VALIDATOR_IMAGE:-ghcr.io/endure-network/endure-subnet-validator:prod}"
    )
    assert miner["image"] == (
        "${MINER_IMAGE:-ghcr.io/endure-network/endure-subnet-miner:prod}"
    )
    env_example = (ROOT / "deploy/operator-node/env.example").read_text()
    for deleted_input in ("SOURCE_SHA=", "\nVALIDATOR_IMAGE=", "\nMINER_IMAGE="):
        assert deleted_input not in env_example
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


def test_operator_deploy_keeps_safeguards_without_release_identity_inputs() -> None:
    deploy_script = (ROOT / "deploy/operator-node/deploy.sh").read_text()

    # One release job retags both images from one commit. Re-checking that on
    # every host verifies Endure's release job, not the operator's deployment.
    for deleted_check in (
        "Refusing mutable image reference",
        "SOURCE_SHA",
        "source_sha",
        "come from different commits",
    ):
        assert deleted_check not in deploy_script
    assert "Expected exactly two runtime images" in deploy_script
    assert "flock -n 9" in deploy_script
    assert "Run this deployment as root." in deploy_script
    assert "previous-images.txt" in deploy_script
    assert "sqlite3.connect" in deploy_script
    assert "PRAGMA integrity_check" in deploy_script
    assert "Refusing mainnet" not in deploy_script
    assert "SERVING_STAGE must be testnet or mainnet" in deploy_script
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
        'if ! "${compose[@]}" up -d --no-build --pull never validator miner-1; then'
        in deploy_script
    )
    assert deploy_script.count("--entrypoint python") == 3
    assert "rendered-compose.yaml" not in deploy_script
    assert "docker compose" in deploy_script
    assert "http://127.0.0.1:8714/live" in deploy_script
    assert "http://127.0.0.1:8714/health" in deploy_script


def test_prod_retag_uses_digest_preserving_copy_for_both_images(tmp_path: Path) -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/publish-prod-images.yml").read_text()
    )
    steps = {step.get("name"): step for step in workflow["jobs"]["publish"]["steps"]}
    command = steps["Retag as prod and semver"]["run"]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args[:3] == ['buildx', 'imagetools', 'create']:
    # Buildx otherwise wraps a single manifest in an index (changing its digest).
    assert '--prefer-index=false' in args, args
    assert args[-1].endswith('@sha256:' + 'a' * 64), args
    tags = [args[i + 1] for i, arg in enumerate(args) if arg == '--tag']
    assert len(tags) == 2 and tags[0].endswith(':prod') and tags[1].endswith(':v0.1.0')
    with open(os.environ['RETAG_LOG'], 'a') as log:
        log.write(args[-1].split('@')[0] + '\\n')
else:
    raise AssertionError(args)
"""
    )
    docker.chmod(0o755)
    log = tmp_path / "retag.log"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", command],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{Path(sys.executable).parent}:" + os.environ["PATH"],
            "TAG_SHA": "b" * 40,
            "RELEASE_TAG": "v0.1.0",
            "VALIDATOR_REPO": "ghcr.io/example/validator",
            "MINER_REPO": "ghcr.io/example/miner",
            "VALIDATOR_DIGEST": "sha256:" + "a" * 64,
            "MINER_DIGEST": "sha256:" + "a" * 64,
            "RETAG_LOG": str(log),
        },
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [
        "ghcr.io/example/validator",
        "ghcr.io/example/miner",
    ]


def _prod_workflow_steps() -> dict[str, dict[str, object]]:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/publish-prod-images.yml").read_text()
    )
    job = workflow["jobs"]["publish"]
    assert job["defaults"]["run"]["shell"] == "bash", (
        "prod publish steps must run under bash with pipefail so a failed "
        "`imagetools inspect | jq` cannot yield an empty digest that verifies"
    )
    return {step.get("name"): step for step in job["steps"]}


def test_prod_publish_resolves_each_soaked_digest_exactly_once() -> None:
    steps = _prod_workflow_steps()
    resolve = str(steps["Resolve the soaked image digests"]["run"])
    retag = str(steps["Retag as prod and semver"]["run"])
    verify = str(steps["Verify the prod channel points at the soaked digests"]["run"])

    assert "VALIDATOR_DIGEST=" in resolve
    assert "MINER_DIGEST=" in resolve
    for later_step in (retag, verify):
        assert "sha-$TAG_SHA" not in later_step
        assert "$VALIDATOR_DIGEST" in later_step
        assert "$MINER_DIGEST" in later_step


VALIDATOR_CHANNEL = "ghcr.io/endure-network/endure-subnet-validator:prod"
MINER_CHANNEL = "ghcr.io/endure-network/endure-subnet-miner:prod"

# A stateful stand-in for the docker CLI. It keeps a registry, a local image
# store, and the two compose containers in a JSON file so a test can move the
# channel between runs and read back what the host ended up running.
FAKE_DOCKER = r"""#!/usr/bin/env python3
import json
import os
import sys

state_path = os.environ["FAKE_DOCKER_STATE"]
with open(state_path) as handle:
    state = json.load(handle)
args = sys.argv[1:]


def finish(code=0):
    with open(state_path, "w") as handle:
        json.dump(state, handle)
    sys.exit(code)


def log(event):
    with open(os.environ["FAKE_DOCKER_EVENTS"], "a") as handle:
        handle.write(event + "\n")


def image_id(reference):
    if reference.startswith("sha256:"):
        return reference
    return state["local"][reference]


def container(container_id):
    for entry in state["containers"].values():
        if entry["id"] == container_id:
            return entry
    sys.exit(f"no such container: {container_id}")


def render(template, entry):
    if ".Name" in template:
        return f'/{entry["id"]}|{entry["ref"]}|{entry["image_id"]}|started'
    if ".State.Health" in template:
        if not entry["running"]:
            return "exited"
        return "unhealthy" if entry["image_id"] in state["unhealthy"] else "healthy"
    if ".State.Running" in template:
        return "true" if entry["running"] else "false"
    if "config-hash" in template:
        return entry["hash"]
    if ".Config.Image" in template:
        return entry["ref"]
    if ".Image" in template:
        return entry["image_id"]
    sys.exit(f"unsupported container format: {template}")


if args[0] == "compose":
    rest = args[5:]  # --env-file <file> -f <file>
    if rest[0] == "config":
        if "--format" in rest:
            wallets = state["wallets"]
            print(json.dumps({"services": {
                name: {
                    "image": reference,
                    "volumes": [{
                        "source": wallets[name],
                        "target": "/root/.bittensor/wallets",
                    }],
                }
                for name, reference in state["services"].items()
            }}))
        elif "--images" in rest:
            print("\n".join(state["services"].values()))
        elif "--hash" in rest:
            for name, digest in state["config_hash"].items():
                print(name, digest)
    elif rest[0] == "ps":
        entry = state["containers"].get(rest[-1])
        if entry:
            print(entry["id"])
    elif rest[0] == "stop":
        log("stop")
        for entry in state["containers"].values():
            entry["running"] = False
    elif rest[0] == "up":
        overrides = {
            "validator": os.environ.get("VALIDATOR_IMAGE"),
            "miner-1": os.environ.get("MINER_IMAGE"),
        }
        state["serial"] += 1
        for name, configured in state["services"].items():
            reference = overrides[name] or configured
            if "never" not in rest and reference in state["registry"]:
                state["local"][reference] = state["registry"][reference]
            state["containers"][name] = {
                "id": f'{name}-{state["serial"]}',
                "ref": reference,
                "image_id": image_id(reference),
                "running": True,
                "hash": state["config_hash"][name],
            }
        log("up " + " ".join(
            f'{name}={entry["image_id"]}'
            for name, entry in state["containers"].items()
        ) + " :: " + " ".join(rest))
elif args[0] == "pull":
    log(f"pull {args[1]}")
    state["local"][args[1]] = state["registry"][args[1]]
    state["pulls"] = state.get("pulls", 0) + 1
    if state["pulls"] % len(state["services"]) == 0:
        state["registry"].update(state.pop("retag_after_pull", {}))
elif args[0] == "image":
    identifier = image_id(args[-1])
    template = args[3]
    if "revision" in template:
        print(state["revisions"][identifier])
    elif "RepoDigests" in template:
        print(f"repo@{identifier}")
    else:
        print(identifier)
elif args[0] == "inspect":
    print(render(args[2], container(args[3])))
elif args[0] == "cp":
    log("cp " + args[-1].rsplit("/", 1)[-1][:9])
    if ":" not in args[-1]:
        with open(args[-1], "w") as handle:
            handle.write("snapshot")
elif args[0] in ("exec", "run"):
    log("restore" if "RESTORE_SOURCE" in " ".join(args) else args[0])
elif args[0] == "volume":
    finish(0 if state.get("volume") else 1)
finish()
"""


class OperatorHost:
    """A throwaway host: fake docker, patched state paths, no root needed."""

    def __init__(self, tmp_path: Path) -> None:
        bash = shutil.which("bash")
        version = subprocess.run(
            [bash or "bash", "-c", "echo ${BASH_VERSINFO[0]}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if int(version) < 4:
            pytest.skip("deploy.sh needs bash 4+ (mapfile)")
        self.bash = str(bash)
        self.root = tmp_path
        self.releases = tmp_path / "releases"
        self.state_path = tmp_path / "docker-state.json"
        self.events_path = tmp_path / "docker-events.log"
        self.events_path.write_text("")
        self.env_file = tmp_path / "operator.env"
        self.env_file.write_text("SERVING_STAGE=testnet\n")
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        self.fake_bin = fake_bin
        for name, body in {
            "docker": FAKE_DOCKER,
            "sleep": "#!/bin/sh\nexit 0\n",
            "curl": (
                "#!/bin/sh\n"
                'while [ "$#" -gt 0 ]; do\n'
                '  if [ "$1" = "--output" ]; then echo "{}" >"$2"; fi\n'
                "  shift\n"
                "done\n"
                "echo 200\n"
            ),
        }.items():
            (fake_bin / name).write_text(body)
            (fake_bin / name).chmod(0o755)
        for wallet in ("validator-wallets", "miner-wallets"):
            (tmp_path / wallet).mkdir()

        script = (ROOT / "deploy/operator-node/deploy.sh").read_text()
        for production, patched, occurrences in (
            ('"/var/lib/endure-node/backups"', f'"{tmp_path}/backups"', 1),
            ('"/var/lib/endure-node/releases"', f'"{self.releases}"', 1),
            ("if ((EUID != 0)); then", "if false; then", 1),
            ("install -d -o root -g root -m 0700", "install -d -m 0700", 2),
        ):
            assert script.count(production) == occurrences, production
            script = script.replace(production, patched)
        self.script = tmp_path / "deploy.sh"
        self.script.write_text(script)

        self.state: dict[str, Any] = {
            "services": {"validator": VALIDATOR_CHANNEL, "miner-1": MINER_CHANNEL},
            "config_hash": {"validator": "hash-v", "miner-1": "hash-m"},
            "wallets": {
                "validator": str(tmp_path / "validator-wallets"),
                "miner-1": str(tmp_path / "miner-wallets"),
            },
            "registry": {},
            "local": {},
            "revisions": {},
            "containers": {},
            "unhealthy": [],
            "serial": 0,
        }
        self.save()

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state))

    def publish(self, revision: str, *, healthy: bool = True) -> tuple[str, str]:
        """Move both channel tags to a new release, as the tag workflow does."""
        self.state = json.loads(self.state_path.read_text())
        identifiers = (f"sha256:validator-{revision}", f"sha256:miner-{revision}")
        for reference, identifier in zip(
            self.state["services"].values(), identifiers, strict=True
        ):
            self.state["registry"][reference] = identifier
            self.state["revisions"][identifier] = revision
            if not healthy:
                self.state["unhealthy"].append(identifier)
        self.save()
        return identifiers

    def deploy(self) -> subprocess.CompletedProcess[str]:
        self.events_path.write_text("")
        result = subprocess.run(
            [self.bash, str(self.script)],
            check=False,
            capture_output=True,
            text=True,
            env={
                "ENDURE_ENV_FILE": str(self.env_file),
                "FAKE_DOCKER_STATE": str(self.state_path),
                "FAKE_DOCKER_EVENTS": str(self.events_path),
                "PATH": os.pathsep.join(
                    [
                        str(self.fake_bin),
                        str(Path(sys.executable).parent),
                        os.environ["PATH"],
                    ]
                ),
            },
        )
        self.state = json.loads(self.state_path.read_text())
        return result

    @property
    def events(self) -> list[str]:
        return self.events_path.read_text().splitlines()

    def running_images(self) -> tuple[str, str]:
        containers = self.state["containers"]
        assert all(entry["running"] for entry in containers.values())
        return containers["validator"]["image_id"], containers["miner-1"]["image_id"]

    def release_records(self) -> list[Path]:
        if not self.releases.exists():
            return []
        return sorted(path for path in self.releases.iterdir() if path.is_dir())


def test_operator_deploy_installs_the_channel_release_on_a_fresh_host(
    tmp_path: Path,
) -> None:
    host = OperatorHost(tmp_path)
    release = host.publish("a" * 40)

    result = host.deploy()

    assert result.returncode == 0, result.stderr
    assert host.running_images() == release
    assert "a" * 40 in result.stdout
    (record,) = host.release_records()
    deployment = (record / "deployment.txt").read_text()
    assert f"REVISION={'a' * 40}" in deployment
    for reference, identifier in zip(
        (VALIDATOR_CHANNEL, MINER_CHANNEL), release, strict=True
    ):
        assert f"IMAGE={reference}|{identifier}|repo@{identifier}" in deployment


def test_operator_deploy_is_a_cheap_no_op_until_the_channel_moves(
    tmp_path: Path,
) -> None:
    host = OperatorHost(tmp_path)
    release = host.publish("a" * 40)
    assert host.deploy().returncode == 0

    result = host.deploy()

    assert result.returncode == 0, result.stderr
    assert host.running_images() == release
    # The timer runs this every few minutes: no snapshot, no restart, no record.
    assert host.events == [f"pull {MINER_CHANNEL}", f"pull {VALIDATOR_CHANNEL}"]
    assert len(host.release_records()) == 1
    assert not list((tmp_path / "backups").iterdir())


def test_operator_deploy_upgrades_with_a_snapshot_when_the_channel_moves(
    tmp_path: Path,
) -> None:
    host = OperatorHost(tmp_path)
    host.publish("a" * 40)
    assert host.deploy().returncode == 0
    upgrade = host.publish("b" * 40)

    result = host.deploy()

    assert result.returncode == 0, result.stderr
    assert host.running_images() == upgrade
    assert "b" * 40 in result.stdout
    assert len(list((tmp_path / "backups").iterdir())) == 1
    assert len(host.release_records()) == 2


def test_operator_deploy_applies_env_changes_while_images_are_current(
    tmp_path: Path,
) -> None:
    host = OperatorHost(tmp_path)
    host.publish("a" * 40)
    assert host.deploy().returncode == 0
    first_validator = host.state["containers"]["validator"]["id"]
    host.state["config_hash"]["validator"] = "hash-v-after-external-ip-edit"
    host.save()

    result = host.deploy()

    assert result.returncode == 0, result.stderr
    assert host.state["containers"]["validator"]["id"] != first_validator
    assert host.state["containers"]["validator"]["hash"] == (
        "hash-v-after-external-ip-edit"
    )


def test_operator_deploy_restarts_a_stopped_node_on_the_current_release(
    tmp_path: Path,
) -> None:
    host = OperatorHost(tmp_path)
    release = host.publish("a" * 40)
    assert host.deploy().returncode == 0
    host.state["containers"]["validator"]["running"] = False
    host.save()

    result = host.deploy()

    assert result.returncode == 0, result.stderr
    assert host.running_images() == release


def test_operator_deploy_rolls_back_by_image_id_and_rejects_the_failed_release(
    tmp_path: Path,
) -> None:
    host = OperatorHost(tmp_path)
    good = host.publish("a" * 40)
    assert host.deploy().returncode == 0
    host.publish("b" * 40, healthy=False)

    failed = host.deploy()

    assert failed.returncode != 0
    # The channel tags now resolve to the failed release, so only the recorded
    # image IDs can bring the previous one back.
    assert host.running_images() == good
    assert "restore" in host.events
    rollback = [event for event in host.events if event.startswith("up ")][-1]
    assert "--pull never" in rollback

    # The next timer run must not redeploy the release that just failed.
    repeated = host.deploy()

    assert repeated.returncode != 0
    assert "b" * 40 in repeated.stderr
    assert "rejected-releases.txt" in repeated.stderr
    assert host.events == [f"pull {MINER_CHANNEL}", f"pull {VALIDATOR_CHANNEL}"]
    assert host.running_images() == good

    fixed = host.publish("c" * 40)

    assert host.deploy().returncode == 0
    assert host.running_images() == fixed


def test_operator_deploy_starts_exactly_the_images_it_pulled(tmp_path: Path) -> None:
    host = OperatorHost(tmp_path)
    pulled = host.publish("a" * 40)
    # The release job retags the channel in the window between pull and start.
    host.state["retag_after_pull"] = {
        VALIDATOR_CHANNEL: "sha256:validator-late",
        MINER_CHANNEL: "sha256:miner-late",
    }
    host.save()

    result = host.deploy()

    assert result.returncode == 0, result.stderr
    assert host.running_images() == pulled
    (record,) = host.release_records()
    assert pulled[0] in (record / "deployment.txt").read_text()


def test_operator_deploy_still_honours_an_operator_digest_pin(tmp_path: Path) -> None:
    host = OperatorHost(tmp_path)
    host.state["services"] = {
        "validator": "ghcr.io/endure-network/endure-subnet-validator@sha256:"
        + "1" * 64,
        "miner-1": "ghcr.io/endure-network/endure-subnet-miner@sha256:" + "2" * 64,
    }
    host.save()
    pinned = host.publish("a" * 40)

    assert host.deploy().returncode == 0
    assert host.running_images() == pinned
    assert host.deploy().returncode == 0
    assert len(host.release_records()) == 1


def test_update_timer_runs_the_deploy_script_from_the_operators_checkout(
    tmp_path: Path,
) -> None:
    node_dir = ROOT / "deploy/operator-node"
    service = (node_dir / "endure-node-update.service").read_text()
    timer = (node_dir / "endure-node-update.timer").read_text()

    assert "Type=oneshot" in service
    assert 'ExecStart="@DEPLOY_DIR@/deploy.sh"' in service
    # Persistent= only applies to calendar timers.
    assert "OnCalendar=*:0/5" in timer
    assert "Persistent=true" in timer
    assert "RandomizedDelaySec=" in timer
    assert "WantedBy=timers.target" in timer

    # The installer fills in its own location: no install path for the
    # operator to choose, remember, or get wrong.
    checkout = tmp_path / "any where" / "operator-node"
    checkout.mkdir(parents=True)
    for name in (
        "install-timer.sh",
        "endure-node-update.service",
        "endure-node-update.timer",
    ):
        (checkout / name).write_text((node_dir / name).read_text())
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    installer = checkout / "install-timer.sh"
    script = installer.read_text()
    for production, patched in (
        ('"/etc/systemd/system"', f'"{unit_dir}"'),
        ("if ((EUID != 0)); then", "if false; then"),
    ):
        assert script.count(production) == 1, production
        script = script.replace(production, patched)
    installer.write_text(script)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "systemctl.log"
    (fake_bin / "systemctl").write_text(f'#!/bin/sh\necho "$*" >>"{calls}"\n')
    (fake_bin / "systemctl").chmod(0o755)

    result = subprocess.run(
        ["bash", str(installer)],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
    )

    assert result.returncode == 0, result.stderr
    installed = (unit_dir / "endure-node-update.service").read_text()
    assert f'ExecStart="{checkout.resolve()}/deploy.sh"' in installed
    assert (unit_dir / "endure-node-update.timer").read_text() == timer
    assert calls.read_text().splitlines() == [
        "daemon-reload",
        "enable --now endure-node-update.timer",
    ]
