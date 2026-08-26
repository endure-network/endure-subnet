from __future__ import annotations

import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from scripts.quality_gates.public_release_scan_models import Finding, ScanError

ALLOWLIST_RULES = frozenset(
    {
        "binary-content",
        "credential-assignment",
        "email",
        "internal-path",
        "ipv4",
        "ipv6",
        "key-path",
        "mnemonic-sequence",
        "pem-key",
        "unsafe-symlink",
    }
)
_WILDCARD_CHARACTERS = frozenset("*?[]")
PRIVATE_MANIFEST_MODE = 0o600


@dataclass(frozen=True, slots=True)
class AllowlistEntry:
    rule: str
    path: str
    value: str
    reason: str


@dataclass(frozen=True, slots=True)
class PrivateRecord:
    atom: str
    mode: str
    source: str


@dataclass(frozen=True, slots=True)
class ScanPolicy:
    entries: tuple[AllowlistEntry, ...]
    private_records: tuple[PrivateRecord, ...]
    errors: tuple[ScanError, ...]


def _policy_error(detail: str) -> ScanError:
    return ScanError("manifest-invalid", detail)


def _read_toml(path: Path, label: str) -> tuple[str | None, tuple[ScanError, ...]]:
    try:
        contents = path.read_bytes()
    except FileNotFoundError:
        return None, (_policy_error(f"{label} is missing"),)
    except OSError as exc:
        return None, (
            _policy_error(
                f"could not read {label}: {exc.strerror or exc.__class__.__name__}"
            ),
        )
    try:
        return contents.decode("utf-8"), ()
    except UnicodeDecodeError:
        return None, (_policy_error(f"{label} is not valid UTF-8"),)


def _parse_allowlist(
    contents: str,
) -> tuple[tuple[AllowlistEntry, ...], tuple[ScanError, ...]]:
    try:
        document = tomllib.loads(contents)
    except tomllib.TOMLDecodeError:
        return (), (_policy_error("allowlist is not valid TOML"),)
    if set(document) != {"entries"}:
        return (), (
            _policy_error("allowlist contains unknown or missing top-level keys"),
        )
    records = document["entries"]
    if not isinstance(records, list):
        return (), (_policy_error("allowlist entries must be an array"),)

    entries: list[AllowlistEntry] = []
    errors: list[ScanError] = []
    seen: set[tuple[str, str, str]] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "rule",
            "path",
            "value",
            "reason",
        }:
            errors.append(_policy_error("allowlist entry has unknown or missing keys"))
            continue
        rule = record["rule"]
        path = record["path"]
        value = record["value"]
        reason = record["reason"]
        if not all(isinstance(field, str) for field in (rule, path, value, reason)):
            errors.append(_policy_error("allowlist entry fields must be strings"))
            continue
        if rule not in ALLOWLIST_RULES:
            errors.append(_policy_error("allowlist entry references an unknown rule"))
            continue
        if not path or not value or not reason:
            errors.append(_policy_error("allowlist entry fields must be non-empty"))
            continue
        if any(character in _WILDCARD_CHARACTERS for character in (path + value)):
            errors.append(
                _policy_error("allowlist entry path or value contains a wildcard")
            )
            continue
        normalized = PurePosixPath(path)
        if normalized.is_absolute() or ".." in normalized.parts or "\\" in path:
            errors.append(
                _policy_error("allowlist entry path must be a relative POSIX path")
            )
            continue
        key = (rule, path, value)
        if key in seen:
            errors.append(_policy_error("allowlist contains a duplicate exact entry"))
            continue
        seen.add(key)
        entries.append(AllowlistEntry(rule=rule, path=path, value=value, reason=reason))
    return tuple(entries), tuple(sorted(set(errors), key=lambda error: error.detail))


def _parse_private_manifest(
    contents: str,
) -> tuple[tuple[PrivateRecord, ...], tuple[ScanError, ...]]:
    try:
        document = tomllib.loads(contents)
    except tomllib.TOMLDecodeError:
        return (), (_policy_error("private manifest is not valid TOML"),)
    if set(document) != {"records"}:
        return (), (
            _policy_error(
                "private manifest contains unknown or missing top-level keys"
            ),
        )
    records = document["records"]
    if not isinstance(records, list):
        return (), (_policy_error("private manifest records must be an array"),)

    parsed: list[PrivateRecord] = []
    errors: list[ScanError] = []
    atoms: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"atom", "mode", "source"}:
            errors.append(
                _policy_error("private manifest record has unknown or missing keys")
            )
            continue
        atom = record["atom"]
        mode = record["mode"]
        source = record["source"]
        if not all(isinstance(field, str) for field in (atom, mode, source)):
            errors.append(
                _policy_error("private manifest record fields must be strings")
            )
            continue
        if not atom or not source:
            errors.append(
                _policy_error("private manifest atom and source must be non-empty")
            )
            continue
        if mode not in {"literal", "casefold"}:
            errors.append(
                _policy_error("private manifest mode must be literal or casefold")
            )
            continue
        if atom in atoms:
            errors.append(_policy_error("private manifest contains a duplicate atom"))
            continue
        atoms.add(atom)
        parsed.append(PrivateRecord(atom=atom, mode=mode, source=source))
    return tuple(parsed), tuple(sorted(set(errors), key=lambda error: error.detail))


def load_policy(allowlist_path: Path, denylist_path: Path | None) -> ScanPolicy:
    """Load strict public exceptions and an optional mode-600 private manifest."""
    allowlist_text, allowlist_errors = _read_toml(allowlist_path, "allowlist")
    if allowlist_text is None:
        return ScanPolicy((), (), allowlist_errors)
    entries, parse_errors = _parse_allowlist(allowlist_text)
    errors = list(allowlist_errors + parse_errors)
    if denylist_path is None or not denylist_path.exists():
        return ScanPolicy(
            entries, (), tuple(sorted(set(errors), key=lambda error: error.detail))
        )
    if stat.S_IMODE(denylist_path.stat().st_mode) != PRIVATE_MANIFEST_MODE:
        errors.append(_policy_error("private manifest must have mode 600"))
        return ScanPolicy(
            entries, (), tuple(sorted(set(errors), key=lambda error: error.detail))
        )
    manifest_text, manifest_errors = _read_toml(denylist_path, "private manifest")
    if manifest_text is None:
        return ScanPolicy(
            entries,
            (),
            tuple(
                sorted(
                    set(errors + list(manifest_errors)), key=lambda error: error.detail
                )
            ),
        )
    records, parse_errors = _parse_private_manifest(manifest_text)
    return ScanPolicy(
        entries,
        records,
        tuple(
            sorted(
                set(errors + list(manifest_errors) + list(parse_errors)),
                key=lambda error: error.detail,
            )
        ),
    )


def apply_allowlist(
    findings: tuple[Finding, ...], entries: tuple[AllowlistEntry, ...]
) -> tuple[tuple[Finding, ...], int, tuple[ScanError, ...]]:
    """Disposition exact finding triples and report every unused public exception."""
    used_indexes: set[int] = set()
    remaining: list[Finding] = []
    for finding in findings:
        matched_indexes = {
            index
            for index, entry in enumerate(entries)
            if (entry.rule, entry.path, entry.value)
            == (finding.rule, finding.path, finding.value)
        }
        if matched_indexes:
            used_indexes.update(matched_indexes)
        else:
            remaining.append(finding)
    errors = tuple(
        ScanError(
            "missing-tracked-path",
            f"unused allowlist entry: {entry.rule} at {entry.path}",
        )
        for index, entry in enumerate(entries)
        if index not in used_indexes
    )
    return tuple(remaining), len(used_indexes), errors
