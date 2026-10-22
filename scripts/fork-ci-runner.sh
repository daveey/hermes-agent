#!/usr/bin/env bash
# [daveey] fork-only: run the Linux jobs of CI on the self-hosted hermes-hub
# runners (hosts/hermes-hub/gh-runner) because the private fork has no
# GitHub-hosted minutes. Idempotent; re-run after merging upstream:
#   scripts/fork-ci-runner.sh && git commit -am "ci: re-point runners [daveey]"
# Leaves macOS/Windows/arm and ${{ matrix.* }} runners alone (tests-os stays
# red on the fork: tests/ci/test_update_ci_routing.py asserts its wiring).
set -euo pipefail
cd "$(dirname "$0")/../.github/workflows"
FILES=(ci.yaml tests.yml lint.yml js-tests.yml rust-tests.yml remote-backends.yml
  bootstrap-installer.yml e2e-desktop-core.yml e2e-desktop-update.yml
  docs-site-checks.yml history-check.yml contributor-check.yml
  uv-lockfile-check.yml infographic-check.yml profile-artifact-check.yml
  icons-freshness-check.yml case-collision-check.yml blob-size-check.yml
  lazy-deps-guard.yml lockfile-diff.yml prompt-surface-diff.yml
  docker-lint.yml supply-chain-audit.yml)
sed -i.bak -E 's/^([[:space:]]*runs-on:[[:space:]]*)(ubuntu-latest(-32-core)?|ubuntu-24\.04)[[:space:]]*$/\1[self-hosted, hermes-hub]/' "${FILES[@]}"
rm -f ./*.bak

