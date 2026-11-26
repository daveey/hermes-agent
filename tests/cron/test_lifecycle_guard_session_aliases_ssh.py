"""Session-alias regression partition: ssh."""

import json
import os
import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.environments.base_session_env import _alias_names_printf, _split_alias_marker
from tools.terminal_tool_guards import _session_aliases_for_guard, gateway_lifecycle_block

from tests.cron.lifecycle_guard_session_aliases_shared import (
    _FN_BODY,
    _big,
    _CARD_REPRO,
    _STOCK_ALIASES,
    live_kanban_db as live_kanban_db,  # noqa: PLC0414 -- pytest fixture reexport
    _NESTED_FOREIGN_SHELLS,
    _PY_PRINTS,
    _PY_TAKEOVER,
    _MULTI_HOP_DATA_CONSUMERS,
    _MULTI_HOP_SHELLS,
)


# Self-heal lifecycleguard-6d64a5ee3d48 (came back after t_01a1d0ea6db6c158): a Python script
# fed over TWO ssh hops (`ssh hub "ssh worker python3 -" < takeover.py`) was walked as a remote
# login shell's script under unknown alias names, so `sqlite3.connect("<live kanban.db>")` read
# as `NAME ( <db> )` and failed closed ("is a SQLite database open in this gateway process").
# `_remote_reads_stdin` treated every further ssh hop as a stdin shell instead of following it to
# the command that actually reads stdin. A hop chain ending in a data consumer reads the file as
# data; one ending in a shell (or a hop with no command) still walks it as a nested shell.



@pytest.mark.parametrize("shape", _MULTI_HOP_DATA_CONSUMERS)
def test_multi_hop_ssh_data_consumer_reads_stdin_as_data(tmp_path, live_kanban_db, shape):
    """Detection guard for lifecycleguard-6d64a5ee3d48: every ssh hop is followed to the
    command that reads stdin, and a data consumer there never makes the file a shell script."""
    py = tmp_path / "takeover.py"
    py.write_text(_PY_TAKEOVER.format(db=live_kanban_db))
    script = tmp_path / "inner.sh"
    script.write_text(f"t ( bash {live_kanban_db} )\n")
    text = shape.format(db=live_kanban_db, py=py, script=script)
    # Unknown session aliases (None) may rename a heredoc's consumer, so a heredoc body is
    # visible there even for a bare `python3 - <<'PY'` (main's rule); a stdin file is not.
    names_list = (frozenset({"zz"}), frozenset()) if "<<" in shape else (
        frozenset({"zz"}), frozenset(), None)
    for names in names_list:
        assert scan_gateway_lifecycle(text, cwd=str(tmp_path), session_aliases=names) == (
            False, None), (shape, names)


@pytest.mark.parametrize("payload", [
    "cd /tmp && git apply", "bash -c 'cd /tmp && git apply'",
    "bash -lc 'cd /tmp && git apply'", "sh -c 'git apply'",
    "bash -c \"sh -c 'git apply'\"",
])
def test_first_hop_payload_patch_is_data(tmp_path, live_kanban_db, payload):
    import shlex

    patch_file = tmp_path / "fix.patch"
    patch_file.write_text(f"t ( bash {live_kanban_db} )\n")
    command = f"ssh h1 {shlex.quote(payload)} < {patch_file}"
    for names in (frozenset(), frozenset({"zz"}), None):
        assert scan_gateway_lifecycle(command, cwd=str(tmp_path), session_aliases=names) == (
            False, None)


@pytest.mark.parametrize("prefix", ["", "env ", "timeout 5 ", "nice ", "command "])
@pytest.mark.parametrize("flags", ["-c", "-ec", "-o pipefail -c"])
def test_first_host_patch_wrapper_real_bash(tmp_path, live_kanban_db, prefix, flags, monkeypatch):
    import shlex

    from tools.environments.local import build_subprocess_env

    body = f"t ( bash {live_kanban_db} )\n"
    patch_file = tmp_path / "fix.patch"
    patch_file.write_text("diff --git a/proof.txt b/proof.txt\nnew file mode 100644\n"
                          "--- /dev/null\n+++ b/proof.txt\n@@ -0,0 +1 @@\n+" + body)
    payload = f"{prefix}bash {flags} 'git apply'"
    # The parent enforces a hard timeout; this fixture needs only argv/stdin
    # forwarding, not GNU coreutils (absent on the room minis).
    bindir = tmp_path / "bin"
    bindir.mkdir()
    timeout = bindir / "timeout"
    timeout.write_text('#!/bin/sh\nshift\nexec "$@"\n', encoding="utf-8")
    timeout.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    with patch_file.open() as stream:
        result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", payload],
                                stdin=stream, cwd=tmp_path, env=build_subprocess_env(),
                                capture_output=True, text=True, encoding="utf-8",
                                timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "proof.txt").read_text() == body
    command = f"ssh h1 {shlex.quote(payload)} < {patch_file}"
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


def test_first_host_patch_exception_preserves_budget_and_all_segments():
    from cron.lifecycle_guard_nested_shells import _MAX_SSH_HOPS, _remote_reads_stdin

    assert _remote_reads_stdin(["git apply; source /dev/stdin"], 2, first_host=True)
    assert _remote_reads_stdin(["git apply"], _MAX_SSH_HOPS + 1, first_host=True)
    assert not _remote_reads_stdin(["git apply"], _MAX_SSH_HOPS, first_host=True)
    assert _remote_reads_stdin(["git apply"], 2)




@pytest.mark.parametrize("consumer", [
    "python3 -c 'import os; os.execlp(\"bash\", \"bash\")'",
    "python3 -c 'import subprocess; subprocess.run([\"bash\"], check=True)'",
])
@pytest.mark.parametrize("aliases", [frozenset(), frozenset({"zz"}), None])
def test_multi_hop_python_launcher_executes_stdin_as_shell(tmp_path, consumer, aliases):
    """Review 5941029921 B1, executed for real: a `python3 -c` launcher behind a second ssh hop
    hands the stdin file to bash, so the guard keeps walking it (a script over the scan cap
    refuses) instead of reading it as Python data."""
    import shlex
    import subprocess
    huge = tmp_path / "huge.sh"
    huge.write_text("# padding\n" * 130000 + 'printf "REVIEW_MARKER\\n"\n')
    startup = tmp_path / "startup.sh"
    startup.write_text("shopt -s expand_aliases\nalias t='true; '\n")
    script = tmp_path / "input.sh"
    script.write_text(f"t ( bash {shlex.quote(str(huge))} )\n")
    with script.open() as stream:
        ran = subprocess.run(["bash", "-c", consumer], stdin=stream, text=True,
                             capture_output=True, timeout=10, check=False,
                             env={**os.environ, "BASH_ENV": str(startup)})
    assert (ran.returncode, ran.stdout, ran.stderr) == (0, "REVIEW_MARKER\n", "")
    command = ("ssh h1 " + shlex.quote("ssh h2 " + shlex.quote(consumer))
               + " < " + shlex.quote(str(script)))
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path), session_aliases=aliases)
    assert unsafe and refusal is not None and "scan cap" in refusal, (command, aliases, refusal)
    huge.write_text('printf "CLEAN\\n"\n')
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path), session_aliases=aliases) == (
        False, None)


@pytest.mark.parametrize("shape", _MULTI_HOP_SHELLS)
def test_multi_hop_ssh_shell_still_walks_stdin_as_nested_script(tmp_path, live_kanban_db, shape):
    """Controls: a hop chain that ends in a shell reading stdin (or that the walk cannot model)
    keeps walking the file as that remote shell's script, under unknown alias names."""
    script = tmp_path / "inner.sh"
    script.write_text(f"t ( bash {live_kanban_db} )\n")
    text = shape.format(db=live_kanban_db, script=script)
    for names in (frozenset({"zz"}), frozenset(), None):
        unsafe, refusal = scan_gateway_lifecycle(text, cwd=str(tmp_path), session_aliases=names)
        assert unsafe is True and refusal is not None and "SQLite" in refusal, (shape, names)
