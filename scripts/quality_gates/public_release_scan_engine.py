from __future__ import annotations

import hashlib
import ipaddress
import os
import posixpath
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from scripts.quality_gates.public_release_scan_models import (
    Finding,
    ScanConfig,
    ScanError,
    ScanReport,
)
from scripts.quality_gates.public_release_scan_patterns import (
    CREDENTIAL_ASSIGNMENT_PATTERN,
    EMAIL_PATTERN,
    IPV4_CANDIDATE_PATTERN,
    IPV6_CANDIDATE_PATTERN,
    MNEMONIC_SEQUENCE_PATTERN,
    PEM_KEY_PATTERN,
)
from scripts.quality_gates.public_release_scan_policy import (
    PrivateRecord,
    ScanPolicy,
    apply_allowlist,
    load_policy,
)

INTERNAL_PREFIXES = (
    ".reviews/",
    "docs/reviews/",
    "docs/reports/",
    "docs/specs/plans/",
    "docs/superpowers/",
)
KEY_PATH_COMPONENTS = frozenset(
    {
        "wallet",
        "wallets",
        "hotkey",
        "hotkeys",
        "coldkey",
        "coldkeys",
        "mnemonic",
        "seed",
    }
)
KEY_PATH_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".jks"})
ALLOWLIST_RELATIVE_PATH = Path("scripts/quality_gates/public_release_allowlist.toml")


@dataclass(frozen=True, slots=True)
class CandidateResult:
    findings: tuple[Finding, ...]
    errors: tuple[ScanError, ...]


def _line_number(contents: str, offset: int) -> int:
    return contents.count("\n", 0, offset) + 1


def _is_python_double_colon_fragment(contents: str, start: int, end: int) -> bool:
    return (
        start > 0
        and end < len(contents)
        and (contents[start - 1].isalnum() or contents[start - 1] == "_")
        and (contents[end].isalnum() or contents[end] == "_")
    )


def _private_findings(
    path: str, contents: str, records: tuple[PrivateRecord, ...]
) -> Iterable[Finding]:
    folded_contents = contents.casefold()
    for record in records:
        match record.mode:
            case "literal":
                start = contents.find(record.atom)
            case "casefold":
                start = folded_contents.find(record.atom.casefold())
            case _:
                continue
        if start >= 0:
            yield Finding(
                path=path,
                rule="private-denylist",
                location=_line_number(contents, start),
                value=record.atom,
            )


def _text_findings(
    path: str, contents: str, records: tuple[PrivateRecord, ...]
) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for match in EMAIL_PATTERN.finditer(contents):
        value = match.group(0)
        _, domain = value.rsplit("@", maxsplit=1)
        if value.casefold() == "hello@endure.network" or domain.casefold() in {
            "example.com",
            "example.net",
            "example.org",
        }:
            continue
        findings.append(
            Finding(path, "email", _line_number(contents, match.start()), value)
        )
    for match in IPV4_CANDIDATE_PATTERN.finditer(contents):
        value = match.group(0)
        try:
            address = ipaddress.IPv4Address(value)
        except ipaddress.AddressValueError:
            continue
        if address.is_loopback or address.is_unspecified:
            continue
        findings.append(
            Finding(path, "ipv4", _line_number(contents, match.start()), value)
        )
    for match in IPV6_CANDIDATE_PATTERN.finditer(contents):
        value = match.group(0)
        if _is_python_double_colon_fragment(contents, match.start(), match.end()):
            continue
        try:
            address = ipaddress.IPv6Address(value)
        except ipaddress.AddressValueError:
            continue
        if address.is_loopback or address.is_unspecified:
            continue
        if address.is_link_local and "." not in value:
            continue
        findings.append(
            Finding(path, "ipv6", _line_number(contents, match.start()), value)
        )
    for rule, pattern in (
        ("pem-key", PEM_KEY_PATTERN),
        ("credential-assignment", CREDENTIAL_ASSIGNMENT_PATTERN),
        ("mnemonic-sequence", MNEMONIC_SEQUENCE_PATTERN),
    ):
        for match in pattern.finditer(contents):
            findings.append(
                Finding(
                    path, rule, _line_number(contents, match.start()), match.group(0)
                )
            )
    findings.extend(_private_findings(path, contents, records))
    return tuple(findings)


def _path_findings(path: str) -> tuple[Finding, ...]:
    relative = PurePosixPath(path)
    findings: list[Finding] = []
    if path.startswith(INTERNAL_PREFIXES):
        findings.append(Finding(path, "internal-path", 0, path))
    name = relative.name
    key_component = any(part in KEY_PATH_COMPONENTS for part in relative.parts)
    dotenv_name = (
        name == ".env" or name.startswith(".env.")
    ) and name != ".env.example"
    if key_component or dotenv_name or relative.suffix in KEY_PATH_SUFFIXES:
        findings.append(Finding(path, "key-path", 0, path))
    return tuple(findings)


def _unsafe_symlink(path: str, target: str) -> Finding | None:
    target_path = PurePosixPath(target)
    if target_path.is_absolute():
        return Finding(path, "unsafe-symlink", 0, target)
    normalized = posixpath.normpath(
        posixpath.join(PurePosixPath(path).parent.as_posix(), target)
    )
    if normalized == ".." or normalized.startswith("../"):
        return Finding(path, "unsafe-symlink", 0, target)
    return None


def _regular_file_result(
    full_path: Path, relative_path: str, policy: ScanPolicy
) -> CandidateResult:
    try:
        contents = full_path.read_bytes()
    except OSError as exc:
        return CandidateResult(
            (),
            (
                ScanError(
                    "read-error",
                    f"could not read {relative_path}: {exc.strerror or exc.__class__.__name__}",
                ),
            ),
        )
    if b"\0" in contents:
        digest = hashlib.sha256(contents).hexdigest()
        return CandidateResult(
            (Finding(relative_path, "binary-content", 0, digest),), ()
        )
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError:
        digest = hashlib.sha256(contents).hexdigest()
        return CandidateResult(
            (Finding(relative_path, "binary-content", 0, digest),), ()
        )
    return CandidateResult(
        _text_findings(relative_path, text, policy.private_records), ()
    )


def _symlink_result(
    full_path: Path, relative_path: str, policy: ScanPolicy
) -> CandidateResult:
    try:
        target = os.readlink(full_path)
    except OSError as exc:
        return CandidateResult(
            (),
            (
                ScanError(
                    "read-error",
                    f"could not read symlink {relative_path}: {exc.strerror or exc.__class__.__name__}",
                ),
            ),
        )
    findings = list(_text_findings(relative_path, target, policy.private_records))
    unsafe = _unsafe_symlink(relative_path, target)
    if unsafe is not None:
        findings.append(unsafe)
    return CandidateResult(tuple(findings), ())


def _candidate_result(
    config: ScanConfig, relative_path: Path, policy: ScanPolicy
) -> CandidateResult:
    path = relative_path.as_posix()
    full_path = config.root / relative_path
    try:
        mode = full_path.lstat().st_mode
    except FileNotFoundError:
        return CandidateResult(
            (), (ScanError("missing-tracked-path", f"tracked path is missing: {path}"),)
        )
    except OSError as exc:
        return CandidateResult(
            (),
            (
                ScanError(
                    "read-error",
                    f"could not inspect {path}: {exc.strerror or exc.__class__.__name__}",
                ),
            ),
        )
    findings = list(_path_findings(path))
    if stat.S_ISREG(mode):
        result = _regular_file_result(full_path, path, policy)
    elif stat.S_ISLNK(mode):
        result = _symlink_result(full_path, path, policy)
    else:
        return CandidateResult(tuple(findings), ())
    findings.extend(result.findings)
    return CandidateResult(tuple(findings), result.errors)


def scan_paths(config: ScanConfig, paths: tuple[Path, ...]) -> ScanReport:
    """Scan a fixed candidate set and apply only exact public exceptions."""
    policy = load_policy(config.allowlist_path, config.denylist_path)
    if policy.errors:
        return ScanReport((), policy.errors, len(policy.entries), 0)
    findings: list[Finding] = []
    errors: list[ScanError] = []
    allowlist_path = ALLOWLIST_RELATIVE_PATH.as_posix()
    for relative_path in paths:
        if relative_path.as_posix() == allowlist_path:
            continue
        result = _candidate_result(config, relative_path, policy)
        findings.extend(result.findings)
        errors.extend(result.errors)
    ordered = tuple(
        sorted(
            findings, key=lambda item: (item.path, item.rule, item.location, item.value)
        )
    )
    remaining, used, allowlist_errors = apply_allowlist(ordered, policy.entries)
    return ScanReport(
        remaining,
        tuple(
            sorted(
                set(errors + list(allowlist_errors)),
                key=lambda error: (error.code, error.detail),
            )
        ),
        len(policy.entries),
        used,
    )
