from __future__ import annotations

import re
from typing import Final, Pattern

EMAIL_PATTERN: Final[Pattern[str]] = re.compile(
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.ASCII | re.IGNORECASE
)
IPV4_CANDIDATE_PATTERN: Final[Pattern[str]] = re.compile(
    r"(?<![0-9A-Fa-f:.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])", re.ASCII
)
IPV6_CANDIDATE_PATTERN: Final[Pattern[str]] = re.compile(
    r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f:.]+(?![0-9A-Fa-f])",
    re.ASCII,
)
PEM_KEY_PATTERN: Final[Pattern[str]] = re.compile(
    r"-----BEGIN (?:[A-Za-z][A-Za-z0-9 _-]* )?PRIVATE KEY-----", re.ASCII
)
CREDENTIAL_ASSIGNMENT_PATTERN: Final[Pattern[str]] = re.compile(
    r"(?:\b|[\"'])[A-Z_][A-Z0-9_]*(?:mnemonic|seed_phrase|private_key|secret_key|api_key|token|password)[\"']?[ \t]*[:=][ \t]*[\"']?[^\s${}\"'<>]{8,}",
    re.ASCII | re.IGNORECASE,
)
MNEMONIC_SEQUENCE_PATTERN: Final[Pattern[str]] = re.compile(
    r"(?i:\bmnemonic\b|\bseed phrase\b)\s*(?:[:=]\s*)?(?:[a-z]+(?:\s+[a-z]+){11}|[a-z]+(?:\s+[a-z]+){14}|[a-z]+(?:\s+[a-z]+){17}|[a-z]+(?:\s+[a-z]+){20}|[a-z]+(?:\s+[a-z]+){23})(?![a-z]|\s+[a-z])",
    re.ASCII,
)
