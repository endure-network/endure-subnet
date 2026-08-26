from __future__ import annotations

from pathlib import Path

import pytest

from scripts.quality_gates.public_release_scan import ScanConfig, scan_directory


def _write_allowlist(root: Path, entries: str = "entries = []\n") -> Path:
    allowlist_path = root / "scripts/quality_gates/public_release_allowlist.toml"
    allowlist_path.parent.mkdir(parents=True, exist_ok=True)
    allowlist_path.write_text(entries, encoding="utf-8")
    return allowlist_path


def _scan(root: Path) -> tuple[str, ...]:
    allowlist_path = _write_allowlist(root)
    report = scan_directory(
        ScanConfig(root=root, allowlist_path=allowlist_path, denylist_path=None)
    )
    return tuple(finding.rule for finding in report.findings)


def test_scan_directory_finds_internal_and_key_paths(tmp_path: Path) -> None:
    # Given: paths reserved for internal artifacts and wallet material.
    (tmp_path / ".reviews").mkdir()
    (tmp_path / ".reviews/report.txt").write_text("clean", encoding="utf-8")
    (tmp_path / "wallet").mkdir()
    (tmp_path / "wallet/config.txt").write_text("clean", encoding="utf-8")
    (tmp_path / ".env").write_text("clean", encoding="utf-8")
    (tmp_path / ".env.example").write_text("clean", encoding="utf-8")
    (tmp_path / "certificate.pem").write_text("clean", encoding="utf-8")

    # When: the scanner inspects the directory.
    rules = _scan(tmp_path)

    # Then: it reports internal and key-bearing paths, but preserves the example exception.
    assert rules.count("internal-path") == 1
    assert rules.count("key-path") == 3


def test_scan_directory_finds_disallowed_email_and_allows_examples(
    tmp_path: Path,
) -> None:
    # Given: one private address and all documented address exceptions.
    private_address = "person" + "@" + "private.test"
    allowed_contact = "hello" + "@" + "endure.network"
    allowed_example = "operator" + "@" + "example.org"
    (tmp_path / "contacts.txt").write_text(
        f"{private_address} {allowed_contact} {allowed_example}", encoding="utf-8"
    )

    # When: the scanner inspects the directory.
    rules = _scan(tmp_path)

    # Then: only the disallowed address is reported.
    assert rules == ("email",)


def test_scan_directory_finds_disallowed_ipv4_and_allows_loopback_and_unspecified(
    tmp_path: Path,
) -> None:
    # Given: a non-allowed address alongside the two accepted address classes.
    private_address = "10.24.0." + "8"
    loopback = "127.0.0." + "1"
    unspecified = "0.0.0." + "0"
    (tmp_path / "network.txt").write_text(
        f"{private_address} {loopback} {unspecified}", encoding="utf-8"
    )

    # When: the scanner inspects the directory.
    rules = _scan(tmp_path)

    # Then: it reports only the non-allowed address.
    assert rules == ("ipv4",)


def test_scan_directory_finds_ipv6_and_allows_local_ipv6_addresses(
    tmp_path: Path,
) -> None:
    # Given: a global all-hex address alongside exempt local address classes.
    all_hex_global = "dead:beef:" + "face:cafe:feed:bead:abba:feed"
    adjacent_global_addresses = (
        "host_" + "2001:db8:" + ":1",
        "2001:db8:" + ":dead:beefX",
        "2001:db8:" + ":dead:beef-tag",
    )
    (tmp_path / "network.txt").write_text(
        " ".join(
            (
                "2001:db8::8",
                all_hex_global,
                *adjacent_global_addresses,
                "[::1]",
                "::",
                "fe80::1",
            )
        ),
        encoding="utf-8",
    )

    # When: the scanner inspects global and exempt IPv6 candidates.
    rules = _scan(tmp_path)

    # Then: every global address is found while loopback, unspecified, and link-local stay exempt.
    assert rules == ("ipv6",) * 5


def test_scan_directory_finds_ipv6_with_embedded_ipv4_tails(tmp_path: Path) -> None:
    # Given: public addresses whose IPv4 tails must remain part of the match.
    addresses = (
        "64:ff9b::8.8.8.8",
        "::ffff:8.8.8.8",
        "2600:1f18:abc:def:1:2:198.51.100.7",
        "fe80::198.51.100.7",
    )
    (tmp_path / "network.txt").write_text(" ".join(addresses), encoding="utf-8")
    allowlist_path = _write_allowlist(tmp_path)

    # When: the scanner inspects the dotted-tail addresses.
    report = scan_directory(
        ScanConfig(
            root=tmp_path,
            allowlist_path=allowlist_path,
            denylist_path=None,
        )
    )

    # Then: every full IPv6 value is detected, including the link-local prefix.
    assert {finding.value for finding in report.findings} == set(addresses)


@pytest.mark.parametrize(
    "contents",
    (
        "tests/path::Class::test",
        "endure/scoring/weights.py::ema_update",
        "ignore::DeprecationWarning:package.*",
        "word::word",
    ),
)
def test_scan_directory_ignores_python_double_colon_syntax(
    tmp_path: Path, contents: str
) -> None:
    # Given: Python and pytest syntax containing a double-colon separator.
    (tmp_path / "syntax.txt").write_text(contents, encoding="utf-8")

    # When: the scanner inspects the syntax.
    rules = _scan(tmp_path)

    # Then: partial hexadecimal fragments are not treated as IPv6 addresses.
    assert rules == ()


def test_scan_directory_finds_pem_credentials_and_mnemonics(tmp_path: Path) -> None:
    # Given: content matching each credential-bearing text rule.
    pem = "-----" + "BEGIN " + "RSA " + "PRIVATE KEY-----"
    credential_name = "service_api" + "_key"
    credential_value = "a" * 8
    phrase = " ".join(["alpha"] * 12)
    (tmp_path / "secrets.txt").write_text(
        f"{pem}\n{credential_name}={credential_value}\nseed phrase: {phrase}\n",
        encoding="utf-8",
    )

    # When: the scanner inspects the directory.
    rules = _scan(tmp_path)

    # Then: every credential-bearing rule produces one finding.
    assert rules == ("credential-assignment", "mnemonic-sequence", "pem-key")


def test_scan_directory_finds_quoted_credential_assignments(tmp_path: Path) -> None:
    # Given: JSON- and mapping-style quoted credential keys.
    json_key = "tiingo" + "_token"
    mapping_key = "service_api" + "_key"
    credential_value = "test" + "-token"
    (tmp_path / "secrets.txt").write_text(
        f"\"{json_key}\": \"{credential_value}\"\n'{mapping_key}': '{credential_value}'\n",
        encoding="utf-8",
    )

    # When: the scanner inspects quoted-key assignments.
    rules = _scan(tmp_path)

    # Then: both credential values receive the assignment rule.
    assert rules == ("credential-assignment", "credential-assignment")


def test_scan_directory_allows_credential_placeholder_values(tmp_path: Path) -> None:
    # Given: an operator template with a non-secret placeholder value.
    (tmp_path / "template.env").write_text(
        "MARKET_DATA_TOKEN=<PLACEHOLDER>\n", encoding="utf-8"
    )

    # When: the scanner inspects the template.
    rules = _scan(tmp_path)

    # Then: placeholders do not resemble a published credential.
    assert rules == ()


@pytest.mark.parametrize("word_count", [11, 13, 14, 16, 17, 19, 20, 22, 23, 25])
def test_scan_directory_rejects_nonstandard_mnemonic_lengths(
    tmp_path: Path, word_count: int
) -> None:
    # Given: a labelled lowercase phrase with an unsupported word count.
    phrase = " ".join(["alpha"] * word_count)
    (tmp_path / "phrase.txt").write_text(f"mnemonic: {phrase}", encoding="utf-8")

    # When: the scanner inspects the directory.
    rules = _scan(tmp_path)

    # Then: no partial phrase is treated as an approved mnemonic length.
    assert "mnemonic-sequence" not in rules


def test_scan_directory_rejects_binary_without_exact_hash_exception(
    tmp_path: Path,
) -> None:
    # Given: a binary file containing an invalid UTF-8 byte.
    binary_path = tmp_path / "artifact.bin"
    binary_path.write_bytes(b"\xff")

    # When: the scanner inspects the directory.
    rules = _scan(tmp_path)

    # Then: binary content is undispositioned.
    assert rules == ("binary-content",)


def test_scan_directory_rejects_nul_containing_file_without_exact_hash_exception(
    tmp_path: Path,
) -> None:
    # Given: a UTF-8 decodable file containing a NUL byte.
    (tmp_path / "nul.bin").write_bytes(b"safe\0text")

    # When: the scanner inspects the directory.
    rules = _scan(tmp_path)

    # Then: NUL-containing content receives the same binary disposition rule.
    assert rules == ("binary-content",)


def test_scan_directory_accepts_binary_with_exact_hash_exception(
    tmp_path: Path,
) -> None:
    # Given: a binary file with its exact SHA-256 allowlist exception.
    binary_path = tmp_path / "artifact.bin"
    binary_path.write_bytes(b"\xff")
    from hashlib import sha256

    digest = sha256(binary_path.read_bytes()).hexdigest()
    _write_allowlist(
        tmp_path,
        f"""[[entries]]
rule = "binary-content"
path = "artifact.bin"
value = "{digest}"
reason = "fixture exercises binary exception handling"
""",
    )

    # When: the scanner inspects the directory.
    report = scan_directory(
        ScanConfig(
            root=tmp_path,
            allowlist_path=tmp_path
            / "scripts/quality_gates/public_release_allowlist.toml",
            denylist_path=None,
        )
    )

    # Then: the exact exception is consumed and no finding remains.
    assert report.findings == ()
    assert report.allowlist_entries_used == 1
