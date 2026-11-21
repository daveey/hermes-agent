"""A retiring backend publishes its retirement so another process can tell a stale owner from a live one. [daveey]

A wake refused "has another live owner" by a Desktop backend about to restart on new code waited out the waker's
long refused backoff (hub 2026-11-21). ``retire_host_loops`` now writes ``run/host-loops/retiring/<pid>.json``;
``retiring_reason(pid)`` reads it for a live pid only, and ``resume_host_loops`` withdraws it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from hermes_cli import host_loop_lease as hll


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(hll, "lease_dir", lambda home=None: tmp_path / "host-loops")
    monkeypatch.setattr(hll, "_handoff_replays", lambda: None)
    with hll._RETIRE_MU:
        hll._RETIRED.clear()
    yield
    with hll._RETIRE_MU:
        hll._RETIRED.clear()


def test_retiring_is_published_and_withdrawn():
    me = os.getpid()
    assert hll.retiring_reason(me) is None

    hll.retire_host_loops("code-skew", "boot abc behind def")
    assert hll.retiring_reason(me) == "boot abc behind def"

    hll.retire_host_loops("always-on-superseded", "superseded")  # a second reason keeps it retiring
    assert hll.retiring_reason(me) == "superseded"
    assert hll.resume_host_loops("code-skew") is False
    assert hll.retiring_reason(me) == "superseded"

    assert hll.resume_host_loops("always-on-superseded") is True
    assert hll.retiring_reason(me) is None


def test_a_record_left_by_a_dead_process_is_ignored(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    path = tmp_path / "host-loops" / "retiring" / f"{child.pid}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pid": child.pid, "start": None, "reason": "code-skew"}), encoding="utf-8")

    assert hll.retiring_reason(child.pid) is None


def test_garbage_answers_none(tmp_path):
    path = tmp_path / "host-loops" / "retiring" / f"{os.getpid()}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{torn", encoding="utf-8")
    assert hll.retiring_reason(os.getpid()) is None
    assert hll.retiring_reason("not-a-pid") is None
    assert hll.retiring_reason(-1) is None
