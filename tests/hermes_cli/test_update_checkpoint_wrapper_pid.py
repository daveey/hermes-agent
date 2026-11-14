"""A checkpoint ACK from the gateway child of a wrapper ``ExecStart`` is the unit's gateway.

hermes-worker-5, 2026-11-12 15:15-15:18Z: ``hermes-gateway.service`` runs ``hermes-gateway-broker``
(``github_quota.py … -- token-broker exec … -- hermes gateway run``), so the unit's ``MainPID`` (the pause
entry's pid) is the wrapper and the gateway, which answers ``checkpoint-for-update`` with its own
``os.getpid()``, is its child. ``_valid_checkpoint_ack`` required ``ack["pid"] == entry["pid"]``: the gateway
accepted the checkpoint and drained its Kanban workers, the updater called the ACK unbelievable, deferred
("busy or cannot acknowledge an idle pause") and restarted the gateway on the old code, four times in a row.
These tests use a real process tree for the wrapper and its children.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from hermes_cli import admission_fence
from hermes_cli import update_cmd_posix_pause as pause
from tests.waiting import wait_until

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc process tree")

psutil = pytest.importorskip("psutil")

NONCE = "episode-wrapper"


def _children_by_script(children):
    names = {}
    for child in children:
        # A child may be in exec: validate and index the same /proc snapshot.
        argv = child.cmdline()
        if len(argv) >= 2:
            names[Path(argv[1]).name] = child.pid
    return names


def test_child_discovery_uses_one_argv_snapshot():
    from unittest.mock import Mock

    # /proc cmdline can become empty between reads while a child execs.
    gateway = Mock(pid=101)
    gateway.cmdline.side_effect = [[sys.executable, "/fixture/main.py"], []]
    worker = Mock(pid=102)
    worker.cmdline.side_effect = [[sys.executable, "/fixture/card_worker.py"], []]
    starting = Mock(pid=103)
    starting.cmdline.return_value = []
    short = Mock(pid=104)
    short.cmdline.return_value = [sys.executable]
    assert _children_by_script([gateway, worker, starting, short]) == {
        "main.py": 101, "card_worker.py": 102,
    }
    for child in (gateway, worker, starting, short):
        child.cmdline.assert_called_once_with()


@pytest.fixture()
def wrapper_tree(tmp_path):
    """``wrapper`` (the unit's MainPID) -> [a ``gateway run`` child, a non-gateway card worker child]."""
    main_py = tmp_path / "hermes_cli" / "main.py"
    main_py.parent.mkdir()
    main_py.write_text("import time\ntime.sleep(60)\n")
    worker_py = tmp_path / "card_worker.py"
    worker_py.write_text("import time\ntime.sleep(60)\n")
    wrapper_py = tmp_path / "broker_wrapper.py"
    wrapper_py.write_text(textwrap.dedent(f"""
        import subprocess, sys, time
        subprocess.Popen([sys.executable, {str(main_py)!r}, "gateway", "run"])
        subprocess.Popen([sys.executable, {str(worker_py)!r}])
        time.sleep(60)
    """))
    wrapper = subprocess.Popen([sys.executable, str(wrapper_py)])
    try:
        def started():
            children = psutil.Process(wrapper.pid).children()
            names = _children_by_script(children)
            return names if set(names) == {"main.py", "card_worker.py"} else None
        by_script = wait_until(started, timeout=10, what="the wrapper's two children")
        yield wrapper.pid, by_script["main.py"], by_script["card_worker.py"]
    finally:
        for child in psutil.Process(wrapper.pid).children(recursive=True):
            child.kill()
        wrapper.kill()
        wrapper.wait(timeout=10)


@pytest.fixture()
def episode(tmp_path, monkeypatch):
    """A live explicit checkpoint intent under a held admission fence (what hermes-sync publishes)."""
    home = tmp_path / ".hermes"
    (home / "core-update").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    now = time.time()
    (home / "core-update/intent").write_text(json.dumps({
        "pid": os.getpid(), "kind": "unfenced", "checkpoint": True, "cron_cut": True,
        "created_at": now, "expires_at": now + 120, "nonce": NONCE}))
    admission_fence.set_hold(home)
    return home


def ask_with_ack(monkeypatch, home, main_pid, ack_pid):
    from gateway import control_socket
    ack = {"pausing": True, "checkpoint": True, "pid": ack_pid, "drain_timeout": 70.0, "nonce": NONCE}
    monkeypatch.setattr(control_socket, "query_gateway_control", lambda *a, **k: dict(ack))
    entry = {"pid": main_pid, "home": str(home), "kind": "systemd", "unit": "hermes-gateway.service"}
    return pause._ask_to_drain(entry), entry


def test_an_ack_from_the_gateway_child_of_the_wrapper_main_pid_is_believed(wrapper_tree, episode, monkeypatch):
    main_pid, gateway_pid, _ = wrapper_tree
    result, entry = ask_with_ack(monkeypatch, episode, main_pid, gateway_pid)
    assert result is True and entry["checkpoint_drain_timeout"] == 70.0


def test_an_ack_from_a_non_gateway_child_of_the_wrapper_is_not(wrapper_tree, episode, monkeypatch):
    main_pid, _, worker_pid = wrapper_tree
    result, entry = ask_with_ack(monkeypatch, episode, main_pid, worker_pid)
    assert result is None and "checkpoint_drain_timeout" not in entry


def test_an_ack_from_a_gateway_outside_the_entrys_tree_is_not(wrapper_tree, episode, monkeypatch):
    _, gateway_pid, worker_pid = wrapper_tree
    # The card worker is not the gateway's ancestor: the wrapper's gateway child is not *its* gateway.
    result, entry = ask_with_ack(monkeypatch, episode, worker_pid, gateway_pid)
    assert result is None and "checkpoint_drain_timeout" not in entry
