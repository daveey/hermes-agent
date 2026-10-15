"""A gateway run by an interpreter whose path contains a space must still be found by the scan.

The mini's launchd gateway runs as ``~/Applications/Hermes Python.app/Contents/MacOS/python3.14 -I -c
"import sys, runpy; …; sys.argv = [… 'gateway', 'run', '--external-supervisor']; runpy.run_path(…)"``.
``ps -o command=`` joins argv with spaces, so the interpreter path splits in two, the inline-source
walk stops at ``Python.app/…`` and the strict matcher rejected the row. ``find_gateway_pids`` then
returned ``[]`` and ``hermes update`` never restarted the gateway through the fleet path.
"""

from unittest.mock import MagicMock, patch

import pytest

import hermes_cli.gateway as gateway_mod
from gateway.status import argv_to_command_line, looks_like_gateway_command_line

_PY = "/Users/me/Applications/Hermes Python.app/Contents/MacOS/python3.14"
_SRC = (
    "import sys, runpy; sys.path.insert(0, '/Users/me/code/hermes-agent'); "
    "sys.argv = ['/Users/me/code/hermes-agent/.venv/bin/hermes', 'gateway', 'run', '--external-supervisor']; "
    "runpy.run_path('/Users/me/code/hermes-agent/.venv/bin/hermes', run_name='__main__')"
)
_ARGV = [_PY, "-I", "-c", _SRC]
_PS_ROW = " ".join(_ARGV)  # what ``ps -Aww -o pid=,command=`` prints: argv boundaries lost


def test_exact_argv_of_runpy_bootstrap_under_spaced_interpreter_is_a_gateway():
    assert looks_like_gateway_command_line(argv_to_command_line(_ARGV))


def test_argv_to_command_line_drops_trailing_nul_part_and_keeps_plain_argv():
    assert argv_to_command_line(["python", "-m", "hermes_cli.main", "gateway", "run", ""]) == (
        "python -m hermes_cli.main gateway run"
    )


def test_non_gateway_inline_source_under_spaced_interpreter_stays_rejected():
    """Control: the #107002 rule holds — an arbitrary ``-c`` program carrying a gateway argv is not one."""
    argv = [_PY, "-c", "import subprocess, sys; subprocess.run(sys.argv[1:])", "hermes", "gateway", "run"]
    assert not looks_like_gateway_command_line(argv_to_command_line(argv))


@pytest.mark.platforms("linux", "macos")
def test_ps_scan_rereads_exact_argv_for_space_split_gateway_rows():
    ps_out = f"4242 {_PS_ROW}\n4343 /usr/bin/python3 -m some_other_thing gateway-ish\n"
    reads = []

    def _exact(pid):
        reads.append(pid)
        return argv_to_command_line(_ARGV) if pid == 4242 else "/usr/bin/python3 -m some_other_thing gateway-ish"

    with (
        patch("os.path.isdir", side_effect=lambda p: p != "/proc"),
        patch("hermes_cli.gateway._get_ancestor_pids", return_value=set()),
        patch("hermes_cli.gateway_migrate_guards.pid_is_other_users", return_value=False),
        patch("gateway.status._read_process_cmdline", side_effect=_exact),
        patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=ps_out, stderr="")),
    ):
        assert gateway_mod._scan_gateway_pids(set(), all_profiles=True) == [4242]
    assert 4242 in reads


@pytest.mark.platforms("linux", "macos")
def test_ps_scan_does_not_reread_rows_without_gateway_token():
    """Cost control: only candidate rows pay the per-pid argv read."""
    ps_out = "5151 /bin/zsh -l\n"
    with (
        patch("os.path.isdir", side_effect=lambda p: p != "/proc"),
        patch("hermes_cli.gateway._get_ancestor_pids", return_value=set()),
        patch("gateway.status._read_process_cmdline") as read,
        patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=ps_out, stderr="")),
    ):
        assert gateway_mod._scan_gateway_pids(set(), all_profiles=True) == []
    read.assert_not_called()


@pytest.mark.platforms("linux")
def test_proc_scan_keeps_argv_boundaries():
    raw = "\x00".join(_ARGV) + "\x00"

    def _open(path, mode="r", **kwargs):
        m = MagicMock()
        m.read.return_value = raw.encode()
        m.__enter__ = lambda s: s
        m.__exit__ = MagicMock(return_value=False)
        return m

    with (
        patch("os.listdir", return_value=["4242"]),
        patch("builtins.open", side_effect=_open),
    ):
        rows = list(gateway_mod._iter_proc_cmdlines(set()))
    assert rows and looks_like_gateway_command_line(rows[0][1])
