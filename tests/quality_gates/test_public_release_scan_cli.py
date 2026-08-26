from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.quality_gates.public_release_scan import main


def _allowlist(root: Path) -> None:
    path = root / "scripts/quality_gates/public_release_allowlist.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("entries = []\n", encoding="utf-8")


def test_main_directory_mode_emits_deterministic_redacted_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given: a directory with a private address assembled only in the fixture.
    address = "person" + "@" + "private.test"
    (tmp_path / "contacts.txt").write_text(address, encoding="utf-8")
    _allowlist(tmp_path)

    # When: directory mode runs twice.
    first_exit = main(["--directory", str(tmp_path)])
    first = capsys.readouterr().out
    second_exit = main(["--directory", str(tmp_path)])
    second = capsys.readouterr().out

    # Then: both reports are byte-stable and do not expose the match.
    assert first_exit == 1
    assert second_exit == 1
    assert first == second
    payload = json.loads(first)
    assert payload["findings"] == [
        {
            "location": 1,
            "path": "contacts.txt",
            "rule": "email",
            "value": "<redacted>",
        }
    ]


def test_main_git_tree_mode_scans_only_tracked_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given: a minimal repository with one tracked clean file and an untracked finding.
    _allowlist(tmp_path)
    (tmp_path / "tracked.txt").write_text("clean", encoding="utf-8")
    address = "person" + "@" + "private.test"
    (tmp_path / "untracked.txt").write_text(address, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    # When: git-tree mode executes against a mocked tracked-path listing.
    monkeypatch.setattr(
        "scripts.quality_gates.public_release_scan.git_tree_paths",
        lambda _: (
            Path("tracked.txt"),
            Path("scripts/quality_gates/public_release_allowlist.toml"),
        ),
    )
    exit_code = main(["--git-tree"])

    # Then: untracked content does not affect the report.
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["finding_count"] == 0


def test_main_directory_mode_catches_every_frozen_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given: one synthetic candidate containing each frozen public-release rule.
    _allowlist(tmp_path)
    (tmp_path / ".reviews").mkdir()
    (tmp_path / ".reviews/internal.txt").write_text("clean", encoding="utf-8")
    (tmp_path / ".env").write_text("clean", encoding="utf-8")
    address = "person" + "@" + "private.test"
    private_address = "10.24.0." + "8"
    pem = "-----" + "BEGIN " + "PRIVATE KEY-----"
    assignment_name = "service_api" + "_key"
    phrase = " ".join(["alpha"] * 12)
    (tmp_path / "findings.txt").write_text(
        f"{address} {private_address}\n{pem}\n{assignment_name}=abcdefgh\nmnemonic: {phrase}\n",
        encoding="utf-8",
    )
    (tmp_path / "unsafe-link").symlink_to("/tmp/outside")

    # When: the public CLI scans the synthetic directory.
    exit_code = main(["--directory", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)

    # Then: every frozen rule produces a redacted finding and a nonzero exit status.
    assert exit_code == 1
    assert {finding["rule"] for finding in payload["findings"]} == {
        "credential-assignment",
        "email",
        "internal-path",
        "ipv4",
        "key-path",
        "mnemonic-sequence",
        "pem-key",
        "unsafe-symlink",
    }
