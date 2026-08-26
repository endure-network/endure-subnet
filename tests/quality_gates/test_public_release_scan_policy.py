from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.quality_gates.public_release_scan import ScanConfig, scan_directory
from scripts.quality_gates.public_release_scan_models import ScanError


def _allowlist(root: Path, contents: str = "entries = []\n") -> Path:
    path = root / "scripts/quality_gates/public_release_allowlist.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    return path


def _config(root: Path, denylist_path: Path | None = None) -> ScanConfig:
    return ScanConfig(
        root=root,
        allowlist_path=_allowlist(root),
        denylist_path=denylist_path,
    )


def test_scan_directory_fails_stale_and_self_matching_allowlist_entries(
    tmp_path: Path,
) -> None:
    # Given: a stale entry and an entry that only matches the allowlist file itself.
    private_address = "person" + "@" + "private.test"
    allowlist = _allowlist(
        tmp_path,
        f"""[[entries]]
rule = "email"
path = "missing.txt"
value = "{private_address}"
reason = "stale fixture"

[[entries]]
rule = "email"
path = "scripts/quality_gates/public_release_allowlist.toml"
value = "{private_address}"
reason = "self matching fixture"
""",
    )

    # When: the scanner inspects the directory.
    report = scan_directory(
        ScanConfig(root=tmp_path, allowlist_path=allowlist, denylist_path=None)
    )

    # Then: both entries remain unused because allowlists never self-satisfy.
    assert report.findings == ()
    assert tuple(error.detail for error in report.errors) == (
        "unused allowlist entry: email at missing.txt",
        "unused allowlist entry: email at scripts/quality_gates/public_release_allowlist.toml",
    )


def test_scan_directory_rejects_wildcard_allowlist(tmp_path: Path) -> None:
    # Given: an allowlist entry with a wildcard path.
    private_address = "person" + "@" + "private.test"
    allowlist = _allowlist(
        tmp_path,
        f"""[[entries]]
rule = "email"
path = "*.txt"
value = "{private_address}"
reason = "invalid wildcard"
""",
    )

    # When: the scanner parses the policy.
    report = scan_directory(
        ScanConfig(root=tmp_path, allowlist_path=allowlist, denylist_path=None)
    )

    # Then: it fails closed before applying the invalid entry.
    assert tuple(error.detail for error in report.errors) == (
        "allowlist entry path or value contains a wildcard",
    )


def test_scan_report_redacts_error_paths_with_structured_reason(tmp_path: Path) -> None:
    # Given: an allowlist error that includes its tracked path internally.
    report = scan_directory(_config(tmp_path))
    report = report.__class__(
        findings=report.findings,
        errors=(
            ScanError(
                "missing-tracked-path",
                "unused allowlist entry: email at private/path.txt",
            ),
        ),
        allowlist_entry_count=report.allowlist_entry_count,
        allowlist_entries_used=report.allowlist_entries_used,
    )

    # When: the scanner serializes its public report.
    payload = report.to_payload()

    # Then: triage retains the reason but never exposes a path.
    assert payload["errors"] == ["missing-tracked-path"]
    assert "private/path.txt" not in payload["errors"]


def test_scan_directory_finds_private_manifest_atoms_in_text_and_symlinks(
    tmp_path: Path,
) -> None:
    # Given: literal and casefold private atoms in text and a symlink target.
    literal_atom = "private" + "-literal-marker"
    folded_atom = "private" + "-casefold-marker"
    (tmp_path / "notes.txt").write_text(
        f"{literal_atom} {folded_atom.upper()}", encoding="utf-8"
    )
    symlink_path = tmp_path / "link"
    symlink_path.symlink_to(f"inside-{literal_atom}")
    manifest_path = tmp_path.parent / f"{tmp_path.name}-public-release-denylist.toml"
    manifest_path.write_text(
        f"""[[records]]
atom = "{literal_atom}"
mode = "literal"
source = "phase-a fixture"

[[records]]
atom = "{folded_atom}"
mode = "casefold"
source = "phase-a fixture"
""",
        encoding="utf-8",
    )
    os.chmod(manifest_path, 0o600)

    # When: the scanner inspects content and symlink targets.
    report = scan_directory(_config(tmp_path, manifest_path))

    # Then: private manifest matches are reported without exposing their values.
    assert tuple(finding.rule for finding in report.findings) == (
        "private-denylist",
        "private-denylist",
        "private-denylist",
    )
    assert report.to_payload()["findings"][0]["value"] == "<redacted>"


@pytest.mark.parametrize(
    "manifest",
    [
        '[[records]]\natom = ""\nmode = "literal"\nsource = "phase-a"\n',
        '[[records]]\natom = "duplicate"\nmode = "literal"\nsource = "phase-a"\n\n[[records]]\natom = "duplicate"\nmode = "casefold"\nsource = "phase-a"\n',
        '[[records]]\natom = "atom"\nmode = "unknown"\nsource = "phase-a"\n',
        '[[records]]\natom = "atom"\nmode = "literal"\nsource = "phase-a"\nextra = "invalid"\n',
    ],
)
def test_scan_directory_fails_closed_for_invalid_private_manifest(
    tmp_path: Path, manifest: str
) -> None:
    # Given: a malformed private manifest.
    manifest_path = tmp_path / "denylist.toml"
    manifest_path.write_text(manifest, encoding="utf-8")
    os.chmod(manifest_path, 0o600)

    # When: the scanner parses the manifest.
    report = scan_directory(_config(tmp_path, manifest_path))

    # Then: the manifest is rejected rather than partially applied.
    assert report.errors
    assert report.findings == ()


def test_scan_directory_rejects_private_manifest_with_incorrect_permissions(
    tmp_path: Path,
) -> None:
    # Given: an otherwise valid manifest that is not mode 600.
    manifest_path = tmp_path / "denylist.toml"
    manifest_path.write_text(
        '[[records]]\natom = "private-atom"\nmode = "literal"\nsource = "phase-a"\n',
        encoding="utf-8",
    )
    os.chmod(manifest_path, 0o644)

    # When: the scanner parses the manifest.
    report = scan_directory(_config(tmp_path, manifest_path))

    # Then: permissions are enforced fail-closed.
    assert tuple(error.detail for error in report.errors) == (
        "private manifest must have mode 600",
    )


def test_scan_directory_rejects_invalid_utf8_private_manifest(tmp_path: Path) -> None:
    # Given: a mode-600 private manifest with invalid UTF-8 bytes.
    manifest_path = tmp_path.parent / f"{tmp_path.name}-invalid-denylist.toml"
    manifest_path.write_bytes(b"\xff")
    os.chmod(manifest_path, 0o600)

    # When: the scanner loads the private manifest.
    report = scan_directory(_config(tmp_path, manifest_path))

    # Then: parsing fails closed without scanning partial policy.
    assert tuple(error.detail for error in report.errors) == (
        "private manifest is not valid UTF-8",
    )


def test_scan_directory_finds_unsafe_symlinks(tmp_path: Path) -> None:
    # Given: absolute and root-escaping symlinks alongside a safe relative one.
    (tmp_path / "target.txt").write_text("clean", encoding="utf-8")
    (tmp_path / "safe-link").symlink_to("target.txt")
    (tmp_path / "absolute-link").symlink_to("/tmp/outside")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "escaping-link").symlink_to("../../outside")

    # When: the scanner inspects symlinks.
    report = scan_directory(_config(tmp_path))

    # Then: only absolute and escaping targets are reported.
    assert tuple(finding.rule for finding in report.findings) == (
        "unsafe-symlink",
        "unsafe-symlink",
    )


def test_scan_directory_excludes_only_git_directory(tmp_path: Path) -> None:
    # Given: a `.git` finding and a matching finding inside another hidden directory.
    address = "person" + "@" + "private.test"
    git_directory = tmp_path / ".git"
    git_directory.mkdir()
    (git_directory / "ignored.txt").write_text(address, encoding="utf-8")
    venv_directory = tmp_path / ".venv"
    venv_directory.mkdir()
    (venv_directory / "included.txt").write_text(address, encoding="utf-8")

    # When: directory mode walks the candidate root.
    report = scan_directory(_config(tmp_path))

    # Then: only the non-git hidden file is scanned.
    assert tuple(finding.path for finding in report.findings) == (".venv/included.txt",)
