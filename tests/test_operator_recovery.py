"""Deployment recovery regressions using the isolated operator host fixture."""

import os
import runpy
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_operator_deploy import ROOT, OperatorHost


def test_interrupted_gate_preserves_recovery_and_never_accepts_matching_images(
    tmp_path: Path,
) -> None:
    host = OperatorHost(tmp_path)
    previous = host.publish("a" * 40)
    assert host.deploy().returncode == 0
    host.publish("b" * 40)
    host.state["interrupt_after_up"] = True
    host.save()
    assert host.deploy().returncode != 0
    pending = host.releases / "pending-deployment"
    record = Path(pending.read_text().strip())
    assert (record / "backup.sha256").is_file()
    assert all(
        image in (record / "previous-images.txt").read_text() for image in previous
    )
    before = host.state["containers"].copy()
    result = host.deploy()
    assert result.returncode != 0
    assert "maintenance recovery" in result.stderr
    assert host.events == []
    assert host.state["containers"] == before
    assert pending.is_file()


def test_transient_failure_does_not_poison_healthy_same_identity(
    tmp_path: Path,
) -> None:
    host = OperatorHost(tmp_path)
    images = host.publish("a" * 40)
    assert host.deploy().returncode == 0
    host.state["config_hash"]["validator"] = "edited-config"
    host.state["health_failures_remaining"] = 1
    host.save()
    assert host.deploy().returncode != 0
    assert host.running_images() == images
    assert not (host.releases / "pending-deployment").exists()
    assert not (host.releases / "rejected-releases.txt").read_text().strip()
    before = host.state["containers"].copy()
    assert host.deploy().returncode == 0
    assert host.state["containers"] == before


def test_unhealthy_current_release_does_not_report_success_or_restart(
    tmp_path: Path,
) -> None:
    host = OperatorHost(tmp_path)
    images = host.publish("a" * 40)
    assert host.deploy().returncode == 0
    host.state["unhealthy"].append(images[0])
    host.save()
    before = host.state["containers"].copy()
    assert host.deploy().returncode != 0
    assert host.state["containers"] == before


def test_adoption_baseline_survives_repeated_deployments(tmp_path: Path) -> None:
    host = OperatorHost(tmp_path)
    legacy = host.publish("a" * 40)
    assert host.deploy().returncode == 0
    # Simulate a host previously managed by the older script.
    (host.releases / "retention-initialized").unlink()
    old_record = host.releases / "0000-legacy"
    old_record.mkdir()
    (old_record / "previous-images.txt").write_text(
        f"validator|local:dev|{legacy[0]}\n"
    )
    saved = tmp_path / "backups" / "validator-predeploy-0000.db"
    saved.write_text("pre-migration database")
    removed = []
    for letter in "bcdef":
        host.publish(letter * 40)
        assert host.deploy().returncode == 0
        removed.extend(event for event in host.events if event.startswith("rmi "))
    assert saved.read_text() == "pre-migration database"
    assert old_record.is_dir()
    assert all(f"rmi {image}" not in removed for image in legacy)
    assert legacy[0] in (host.releases / "protected-images.txt").read_text()


def test_failed_release_images_are_in_cleanup_inventory(tmp_path: Path) -> None:
    host = OperatorHost(tmp_path)
    host.publish("a" * 40)
    assert host.deploy().returncode == 0
    failed = host.publish("b" * 40, healthy=False)
    assert host.deploy().returncode != 0
    host.publish("c" * 40)
    assert host.deploy().returncode == 0
    assert all(f"rmi {image}" in host.events for image in failed)


def test_backup_failure_defers_expensive_retry_and_allows_explicit_retry(
    tmp_path: Path,
) -> None:
    host = OperatorHost(tmp_path)
    host.publish("a" * 40)
    assert host.deploy().returncode == 0
    host.publish("b" * 40)
    host.state["host_backup_disk_full"] = True
    host.save()
    assert host.deploy().returncode != 0
    result = host.deploy()
    assert result.returncode != 0
    assert "Backup retry deferred" in result.stderr
    assert not any(event.startswith(("exec ", "run ", "up ")) for event in host.events)
    host.state["host_backup_disk_full"] = False
    host.save()
    (host.releases / "backup-retry-after").unlink()
    assert host.deploy().returncode == 0


def test_existing_image_pins_are_reported(tmp_path: Path) -> None:
    host = OperatorHost(tmp_path)
    host.state["services"] = {
        "validator": "registry/validator@sha256:111",
        "miner-1": "registry/miner@sha256:222",
    }
    host.save()
    host.publish("a" * 40)
    result = host.deploy()
    assert result.returncode == 0
    assert result.stdout.count("explicit image override") == 2
    assert "follows :testnet" not in result.stdout


@pytest.mark.parametrize("unsafe_kind", ["owner", "write", "symlink"])
def test_installation_validation_checks_ancestors(unsafe_kind: str) -> None:
    validate = runpy.run_path(str(ROOT / "deploy/operator-node/check-installation.py"))[
        "validate_path"
    ]
    safe = os.stat_result((stat.S_IFDIR | 0o755, 0, 0, 1, 0, 0, 0, 0, 0, 0))
    mode = (
        stat.S_IFLNK | 0o777
        if unsafe_kind == "symlink"
        else stat.S_IFDIR | (0o777 if unsafe_kind == "write" else 0o755)
    )
    unsafe = os.stat_result(
        (mode, 0, 0, 1, 1000 if unsafe_kind == "owner" else 0, 0, 0, 0, 0, 0)
    )
    with patch.object(Path, "lstat", side_effect=[safe, safe, unsafe]):
        with pytest.raises(ValueError, match="Unsafe installation path"):
            validate(Path("/opt/endure-node/deploy.sh"))
    with patch.object(Path, "lstat", return_value=safe):
        validate(Path("/opt/endure-node/deploy.sh"))


def test_cleanup_bounds_only_completed_managed_records(tmp_path: Path) -> None:
    host = OperatorHost(tmp_path)
    host.publish("a" * 40)
    assert host.deploy().returncode == 0
    for index in range(25):
        record = host.releases / f"0000-{index:02}"
        record.mkdir()
        (record / "managed").touch()
        (record / "rollback-complete").touch()
    protected = host.releases / "0000-00"
    (protected / "protected").touch()
    host.publish("b" * 40)
    assert host.deploy().returncode == 0
    assert protected.is_dir()
    assert not (host.releases / "0000-01").exists()
    assert (host.releases / "0000-24").is_dir()


def test_channel_retry_keeps_saved_digests_after_partial_failure(
    tmp_path: Path,
) -> None:
    import json
    import subprocess

    import yaml

    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/publish-release-images.yml").read_text()
    )
    job = workflow["jobs"]["channel"]
    assert job["needs"] == "publish"
    assert all("docker build " not in str(step) for step in job["steps"])
    command = next(
        step["run"]
        for step in job["steps"]
        if step.get("name") == "Move the testnet channel"
    )
    manifest = (
        "SOURCE_SHA="
        + "a" * 40
        + "\nVALIDATOR_IMAGE=local/validator@sha256:"
        + "b" * 64
        + "\nMINER_IMAGE=local/miner@sha256:"
        + "c" * 64
        + "\n"
    )
    artifact = tmp_path / "release-images.env"
    artifact.write_text(manifest)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "calls.jsonl"
    fail = tmp_path / "fail-once"
    fail.touch()
    docker = fake_bin / "docker"
    docker.write_text(f"""#!/usr/bin/env python3
import sys,json
from pathlib import Path
args=sys.argv[1:]
with Path({str(log)!r}).open("a") as out: out.write(json.dumps(args)+"\\n")
if args[:3] == ["buildx","imagetools","create"]:
    if "miner" in args[-1] and Path({str(fail)!r}).exists():
        Path({str(fail)!r}).unlink()
        sys.exit(1)
elif args[:3] == ["buildx","imagetools","inspect"]:
    print(json.dumps({{"digest":"sha256:"+("c" if "miner" in args[-1] else "b")*64}}))
else: sys.exit(2)
""")
    docker.chmod(0o755)
    for expected in (1, 0):
        result = subprocess.run(
            ["bash", "-eo", "pipefail", "-c", command],
            cwd=tmp_path,
            env={**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == expected, result.stderr
        assert artifact.read_text() == manifest
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    creates = [call[-1] for call in calls if call[2] == "create"]
    assert creates[:2] == creates[2:]
