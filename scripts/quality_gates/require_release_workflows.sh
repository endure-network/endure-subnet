#!/usr/bin/env bash
set -euo pipefail

source_sha="${1:?source SHA required}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

for workflow in ci.yml devnet-qualification.yml; do
  runs_json="$(gh api --method GET \
    "repos/${GITHUB_REPOSITORY:?}/actions/workflows/$workflow/runs" \
    -f head_sha="$source_sha" -f branch=staging -f event=push -f per_page=20)"
  python3 scripts/quality_gates/release_workflow_run.py \
    --sha "$source_sha" <<<"$runs_json"
done
