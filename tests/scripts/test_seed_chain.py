from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_CHAIN = REPO_ROOT / "scripts" / "dev" / "seed_chain.sh"


def _seed_functions(tmp_path: Path) -> Path:
    functions = tmp_path / "seed-chain-functions.sh"
    functions.write_text(
        SEED_CHAIN.read_text(encoding="utf-8").split("# ---- Step 0:", 1)[0],
        encoding="utf-8",
    )
    return functions


def _run_localnet_guard(
    tmp_path: Path, endpoint: str
) -> subprocess.CompletedProcess[str]:
    functions = _seed_functions(tmp_path)
    return subprocess.run(
        ["bash", "-c", f"source {functions}; require_localnet"],
        cwd=REPO_ROOT,
        env=os.environ | {"BTCLI": "/usr/bin/true", "CHAIN_ENDPOINT": endpoint},
        text=True,
        capture_output=True,
        check=False,
    )


def _run_disable_commit_reveal(
    tmp_path: Path, *, responses: tuple[str, ...]
) -> subprocess.CompletedProcess[str]:
    fake_btcli = tmp_path / "btcli"
    response_file = tmp_path / "responses"
    response_file.write_text("\n---\n".join(responses), encoding="utf-8")
    fake_btcli.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
response_file="${SEED_TEST_RESPONSES:?}"
state_file="${SEED_TEST_STATE:?}"
done_file="${SEED_TEST_DONE:?}"
count=0
if [[ -f "$state_file" ]]; then
  count="$(cat "$state_file")"
fi
printf '%s' "$((count + 1))" >"$state_file"
response="$(awk -v position="$count" '
BEGIN { RS="\\n---\\n" }
NR == position + 1 { print; found = 1; exit }
{ last = $0 }
END { if (!found) print last }
' "$response_file")"
printf '%s\\n' "$response"
if grep -q '"success": true' <<<"$response"; then
  touch "$done_file"
fi
""",
        encoding="utf-8",
    )
    fake_btcli.chmod(0o755)
    state_file = tmp_path / "state"
    done_file = tmp_path / "done"
    functions = _seed_functions(tmp_path)
    command = "\n".join(
        (
            f"source {functions}",
            "NETUID=2",
            "commit_reveal_weights_enabled() {",
            f"  if [[ -f {done_file} ]]; then",
            "    printf 'false\\n'",
            "  else",
            "    printf 'true\\n'",
            "  fi",
            "}",
            "sleep() { SECONDS=$((SECONDS + 1)); }",
            "SECONDS=0",
            "disable_commit_reveal_weights_if_needed",
        )
    )
    environment = os.environ | {
        "BTCLI": str(fake_btcli),
        "PY": "/bin/true",
        "SEED_TEST_RESPONSES": str(response_file),
        "SEED_TEST_STATE": str(state_file),
        "SEED_TEST_DONE": str(done_file),
        "COMMIT_REVEAL_DISABLE_TIMEOUT_SECONDS": "3",
    }
    return subprocess.run(
        ["bash", "-c", command],
        cwd=REPO_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("endpoint", "sensitive"),
    (
        ("".join(("ws://localhost:9946", "@remote.example")), "remote.example"),
        (
            "".join(("ws://user:credential-sentinel", "@remote.example:9946")),
            "credential-sentinel",
        ),
    ),
)
def test_seed_chain_rejects_unsafe_endpoint_without_echoing_it(
    tmp_path: Path, endpoint: str, sensitive: str
) -> None:
    result = _run_localnet_guard(tmp_path, endpoint)

    assert result.returncode == 1
    assert sensitive not in result.stderr
    assert "credential-free localnet address" in result.stderr


@pytest.mark.parametrize(
    "endpoint",
    (
        "ws://127.0.0.1:9946",
        "wss://localhost",
        "ws://[::1]:9946/path",
    ),
)
def test_seed_chain_accepts_canonical_loopback_endpoints(
    tmp_path: Path, endpoint: str
) -> None:
    assert _run_localnet_guard(tmp_path, endpoint).returncode == 0


def test_seed_chain_retries_multiline_protected_window_response(
    tmp_path: Path,
) -> None:
    result = _run_disable_commit_reveal(
        tmp_path,
        responses=(
            '{\n  "error": "AdminActionProhibitedDuringWeightsWindow"\n}',
            '{"success": true}',
        ),
    )

    assert result.returncode == 0
    assert (tmp_path / "state").read_text(encoding="utf-8") == "2"


def test_seed_chain_reports_permanent_sudo_error_without_retry(tmp_path: Path) -> None:
    result = _run_disable_commit_reveal(
        tmp_path,
        responses=('{"error": "BadOrigin"}',),
    )

    assert result.returncode == 1
    assert "BadOrigin" not in result.stderr
    assert "btcli returned a non-retryable error" in result.stderr
    assert (tmp_path / "state").read_text(encoding="utf-8") == "1"


def test_seed_chain_stops_at_the_configured_protected_window_deadline(
    tmp_path: Path,
) -> None:
    result = _run_disable_commit_reveal(
        tmp_path,
        responses=('{"error": "AdminActionProhibitedDuringWeightsWindow"}',),
    )

    assert result.returncode == 1
    assert "timed out" in result.stderr
    assert int((tmp_path / "state").read_text(encoding="utf-8")) <= 3
