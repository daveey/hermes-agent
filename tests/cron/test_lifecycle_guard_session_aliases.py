"""Review 5888336995 nit 2: aliases the terminal backend's login snapshot defines live outside the
walked text (``alias -p`` + ``shopt -s expand_aliases`` in ``_snapshot_bootstrap_script``), yet
every command runs with them. The guard must hear about them from the backend."""

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.environments.base_session_env import _alias_names_printf, _split_alias_marker
from tools.terminal_tool_guards import _session_aliases_for_guard, gateway_lifecycle_block

_FN_BODY = "q() { python3 - <<'PY'\nbash -c 'hermes gateway \"restart\"'\nPY\n}\nq\n"


def _big(tmp_path):
    big = tmp_path / "huge.sh"
    big.write_text("# padding\n" * 130000 + 'hermes gateway "restart"\n')
    return big


def test_reviewer_repro_snapshot_time_alias_fails_closed(tmp_path):
    """`alias t='time '` from the snapshot makes `t ( bash huge.sh )` a subshell running it."""
    command = f"t ( bash {_big(tmp_path)} )"
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)  # no session
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path),
                                             session_aliases=frozenset({"t"}))
    assert unsafe is True and refusal is not None and "scan cap" in refusal


def test_unknown_session_aliases_fail_closed(tmp_path):
    command = f"t ( bash {_big(tmp_path)} )"
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path), session_aliases=None)
    assert unsafe is True and refusal is not None and "scan cap" in refusal


@pytest.mark.parametrize("names,blocked", [
    (frozenset(), False), (frozenset({"ll", "gs"}), False),
    (frozenset({"python3"}), True), (frozenset({"timeout"}), True), (frozenset({"bash"}), True),
    (None, True),
])
def test_session_alias_shadowing_a_consumer_keeps_heredoc_bodies_visible(tmp_path, names, blocked):
    """`alias python3=bash` in the snapshot runs a "Python" heredoc body as shell."""
    assert scan_gateway_lifecycle(_FN_BODY, cwd=str(tmp_path), session_aliases=names)[0] is blocked


def test_bootstrap_alias_names_round_trip_through_real_bash():
    marker = "__HERMES_ALIASES_t__"
    script = ("shopt -s expand_aliases\nalias t='time '\nalias ll='ls -l'\n"
              "alias -- -x='echo'\nalias multi='echo a\necho b'\n" + _alias_names_printf(marker))
    out = subprocess.run(["/bin/bash", "--norc", "--noprofile", "-s"], input=script, text=True,
                         capture_output=True, timeout=10, check=True).stdout
    names = _split_alias_marker(out, marker)
    assert names is not None and {"t", "ll", "-x", "multi"} <= names
    assert _split_alias_marker("no marker here", marker) is None
    empty = subprocess.run(["/bin/bash", "--norc", "--noprofile", "-s"],
                           input=_alias_names_printf(marker), text=True, capture_output=True,
                           timeout=10, check=True).stdout
    assert _split_alias_marker(empty, marker) == frozenset()


def test_guard_reads_backend_hook_on_the_class_only():
    class Backend:
        def __init__(self, names):
            self.names = names

        def lifecycle_guard_session_aliases(self):
            return self.names

    class Broken:
        def lifecycle_guard_session_aliases(self):
            raise AttributeError("_snapshot_ready")

    assert _session_aliases_for_guard(Backend(frozenset({"t"}))) == frozenset({"t"})
    assert _session_aliases_for_guard(Backend(None)) is None
    assert _session_aliases_for_guard(Backend({"t"})) is None  # wrong type: unknown
    assert _session_aliases_for_guard(Broken()) is None
    assert _session_aliases_for_guard(SimpleNamespace(cwd="/")) == frozenset()


def test_local_backend_reports_snapshot_aliases_and_terminal_guard_refuses(tmp_path, monkeypatch):
    """End to end: a login-file alias reaches the snapshot, the backend reports it, and the
    terminal guard refuses the reviewer's repro (it allowed it before the fix)."""
    from tools.environments.local import LocalEnvironment

    init_file = tmp_path / "custom-init.sh"
    init_file.write_text("alias t='time '\n")
    with patch("tools.environments.local._read_terminal_shell_init_config",
               return_value=([str(init_file)], False)):
        env = LocalEnvironment(cwd=str(tmp_path), timeout=15)
        try:
            names = env.lifecycle_guard_session_aliases()
            assert names is not None and "t" in names
            assert "__HERMES_ALIASES_" not in env.execute("echo hi").get("output", "")
            monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process",
                                lambda: True)
            result = gateway_lifecycle_block(
                command=f"t ( bash {_big(tmp_path)} )", env=env, env_type="local",
                cwd=str(tmp_path), workdir=str(tmp_path), session_key="snapshot-alias-test")
        finally:
            env.cleanup()
    assert result is not None and "scan cap" in json.loads(result)["error"]
