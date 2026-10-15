"""``hermes update`` must hand a Hermes-Python.app runpy-bootstrapped ``--external-supervisor`` gateway
back to its supervisor, not arm a detached ``--replace`` relaunch that races the supervisor's respawn.

Live argv of the mini's launchd gateway (``com.daveey.babybear.hermes``): the flag lives INSIDE the
``-c`` source element, and the interpreter path contains a space. ``_capture_gateway_argv`` joined it
with plain spaces, the matcher rejected it, argv came back None, and the update armed a watcher: two
``gateway.start`` lines, the launchd child exited, and the survivor ran outside launchd.
"""

import types

import pytest

from hermes_cli import gateway
from hermes_cli import gateway_supervised_restart as gsr

_PY = "/Users/me/Applications/Hermes Python.app/Contents/MacOS/python3.14"
_HERMES = "/Users/me/code/hermes-agent/.venv/bin/hermes"


def _runpy_argv(*gateway_args: str) -> list[str]:
    argv_list = ", ".join(repr(a) for a in (_HERMES, "gateway", "run", *gateway_args))
    src = (
        "import sys, runpy; sys.path.insert(0, '/Users/me/code/hermes-agent'); "
        f"sys.argv = [{argv_list}]; runpy.run_path({_HERMES!r}, run_name='__main__')"
    )
    return [_PY, "-I", "-c", src]


SUPERVISED = _runpy_argv("--external-supervisor")
UNSUPERVISED = _runpy_argv()


def _fake_psutil(monkeypatch, argv):
    fake = types.SimpleNamespace(Process=lambda pid: types.SimpleNamespace(cmdline=lambda: list(argv)))
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake)


def test_capture_gateway_argv_accepts_runpy_bootstrap_under_spaced_interpreter(monkeypatch):
    _fake_psutil(monkeypatch, SUPERVISED)
    assert gateway._capture_gateway_argv(4242) == SUPERVISED


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (SUPERVISED, True),
        (UNSUPERVISED, False),
        (["python", "-m", "hermes_cli.main", "gateway", "run", "--external-supervisor"], True),
        (None, False),
        # #107002 control: an arbitrary -c program merely carrying the flag is not a declaration.
        ([_PY, "-c", "import subprocess, sys; subprocess.run(sys.argv[1:])", "x --external-supervisor"], False),
    ],
)
def test_argv_declares_external_supervisor(argv, expected):
    assert gateway.argv_declares_external_supervisor(argv) is expected


def test_update_hands_runpy_external_supervisor_gateway_back_without_watcher(monkeypatch):
    _fake_psutil(monkeypatch, SUPERVISED)
    monkeypatch.setattr(
        gateway, "launch_detached_profile_gateway_restart", lambda *_a: pytest.fail("watcher must not be armed")
    )
    monkeypatch.setattr(
        gateway, "launch_detached_gateway_restart_by_cmdline", lambda *_a: pytest.fail("watcher must not be armed")
    )
    assert gateway._prepare_profile_gateway_update_restart("default", 4242) == "external-supervisor"


def test_update_still_arms_watcher_for_unsupervised_runpy_gateway(monkeypatch):
    """Control: without the flag the same bootstrap form keeps the detached relaunch."""
    _fake_psutil(monkeypatch, UNSUPERVISED)
    armed = []
    monkeypatch.setattr(gateway, "launch_detached_profile_gateway_restart", lambda *a: armed.append(a) or True)
    assert gateway._prepare_profile_gateway_update_restart("default", 4242) == "detached"
    assert armed


def test_declares_external_supervisor_reads_runpy_argv_when_identify_is_silent(monkeypatch, tmp_path):
    _fake_psutil(monkeypatch, SUPERVISED)
    monkeypatch.setattr("gateway.control_socket.identify_gateway", lambda _home: None)
    assert gsr.gateway_declares_external_supervisor(4242, home=tmp_path) is True
