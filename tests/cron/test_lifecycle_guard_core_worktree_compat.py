"""Compatibility witnesses from the core-worktree delivery helper."""
from cron.lifecycle_guard import scan_gateway_lifecycle


def test_core_worktree_ssh_quote_fragments_are_not_programs(tmp_path):
    script = tmp_path / "helper.sh"
    script.write_text('''#!/bin/bash
hub_sh() {
  local s="$1"; shift
  ssh some-host "bash -c $(printf '%q' "$s") probe $(printf '%q ' "$@")"
}
s='printf ok'
hub_sh "$s"
''')
    assert scan_gateway_lifecycle(f"bash {script}") == (False, None)
    script.write_text(script.read_text() + "\nhermes gateway restart\n")
    assert scan_gateway_lifecycle(f"bash {script}") == (True, None)


def test_core_worktree_python_watchdog_is_not_unknown_shell(tmp_path):
    script = tmp_path / "helper.sh"
    script.write_text('''#!/bin/bash
PY3="$(command -v /usr/bin/python3 || command -v python3)"
read -r -d '' TRANSFER_BOUND_PY <<'PY' || true
import sys
sys.exit(0)
PY
transfer_bounded() {
  "$PY3" -c "$TRANSFER_BOUND_PY" "$@"
}
transfer_bounded 120
''')
    assert scan_gateway_lifecycle(f"bash {script}") == (False, None)
