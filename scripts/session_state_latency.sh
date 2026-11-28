#!/usr/bin/env bash
# Run the SessionState v2 latency harness under the repo's test interpreter (it has fastapi + ruamel). [daveey]
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -z "${__HERMES_TEST_PYTHON:-}" ]; then
  exec scripts/run-in-hermes-env "$BASH" "$0" --root "${TMPDIR:-/tmp}" "$@"
fi
exec "$__HERMES_TEST_PYTHON" scripts/session_state_latency.py "$@"
