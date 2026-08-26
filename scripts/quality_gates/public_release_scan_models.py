from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict


@dataclass(frozen=True, slots=True)
class ScanConfig:
    """Typed scanner inputs constructed by the CLI or focused tests."""

    root: Path
    allowlist_path: Path
    denylist_path: Path | None


@dataclass(frozen=True, slots=True)
class Finding:
    """Raw finding kept internal until serialized into its redacted form."""

    path: str
    rule: str
    location: int
    value: str


@dataclass(frozen=True, slots=True)
class ScanError:
    code: str
    detail: str


class FindingPayload(TypedDict):
    """Redacted, deterministic finding representation emitted by the scanner."""

    location: int
    path: str
    rule: str
    value: str


class ReportPayload(TypedDict):
    """Stable public JSON report payload."""

    allowlist_entry_count: int
    allowlist_entries_used: int
    error_count: int
    errors: list[str]
    finding_count: int
    findings: list[FindingPayload]


@dataclass(frozen=True, slots=True)
class ScanReport:
    """Scanner result with no raw sensitive value exposed through JSON output."""

    findings: tuple[Finding, ...]
    errors: tuple[ScanError, ...]
    allowlist_entry_count: int
    allowlist_entries_used: int

    @property
    def failed(self) -> bool:
        """Whether undispositioned findings or policy failures remain."""
        return bool(self.findings or self.errors)

    def to_payload(self) -> ReportPayload:
        """Return the canonical redacted JSON payload."""
        findings = [
            FindingPayload(
                location=finding.location,
                path=finding.path,
                rule=finding.rule,
                value="<redacted>",
            )
            for finding in self.findings
        ]
        return ReportPayload(
            allowlist_entry_count=self.allowlist_entry_count,
            allowlist_entries_used=self.allowlist_entries_used,
            error_count=len(self.errors),
            errors=[error.code for error in self.errors],
            finding_count=len(self.findings),
            findings=findings,
        )
