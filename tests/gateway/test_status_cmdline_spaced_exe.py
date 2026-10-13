"""[daveey] A gateway whose interpreter path has a space (``~/Applications/Hermes Python.app``)
must still read as a running gateway: the joined psutil cmdline quotes that argument."""

import sys
import types

from gateway import status


def test_spaced_interpreter_path_still_reads_as_gateway(monkeypatch):
    argv = [
        "/Users/x/Applications/Hermes Python.app/Contents/MacOS/python3.14",
        "-I",
        "/Users/x/.hermes/hermes-agent/hermes_cli/main.py",
        "gateway",
        "run",
        "--external-supervisor",
    ]
    fake = types.SimpleNamespace(Process=lambda pid: types.SimpleNamespace(cmdline=lambda: argv))
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(status.Path, "read_bytes", lambda self: b"")

    cmdline = status._read_process_cmdline(4242) or ""

    assert cmdline.startswith('"/Users/x/Applications/Hermes Python.app/')
    assert status.looks_like_gateway_runtime_command_line(cmdline)
