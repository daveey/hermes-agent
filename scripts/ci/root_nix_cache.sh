#!/usr/bin/env bash
# Keep stable outputs alive through cache-nix-action's post-job GC.
set -euo pipefail
mkdir -p "$RUNNER_TEMP/nix-cache-roots"
nix build .#ci-cache --out-link "$RUNNER_TEMP/nix-cache-roots/stable" --print-build-logs
nix path-info --json --closure-size "$RUNNER_TEMP/nix-cache-roots/stable" > "$RUNNER_TEMP/nix-cache-size.json"
python3 scripts/ci/nix_cache_budget.py "$RUNNER_TEMP/nix-cache-size.json"
