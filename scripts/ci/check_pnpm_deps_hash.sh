#!/usr/bin/env bash
# Called under GNU timeout by nix.yml; the entire validation shares one deadline.
set -euo pipefail

check_hash() {
  if nix build .#pnpm-deps --no-link --print-build-logs "$@" 2>&1 | tee "$RUNNER_TEMP/pnpm-deps.log"; then
    return 0
  fi
  got=$(sed -n 's/^ *got: *\(sha256-[A-Za-z0-9+/=]*\)$/\1/p' "$RUNNER_TEMP/pnpm-deps.log")
  if [ -n "$got" ]; then
    echo "::error file=nix/pnpm-deps-hash::pnpm-lock.yaml changed the pnpm store. Set nix/pnpm-deps-hash to $got (or run: nix run .#update-pnpm-lockfile)"
  fi
  return 1
}

# Materialize first: --rebuild requires an existing output on a cold store.
check_hash
# A fixed-output store path is keyed by the declared hash, not pnpm-lock.yaml.
# Never accept the restored output alone: rebuild and verify its actual bytes.
check_hash --rebuild
