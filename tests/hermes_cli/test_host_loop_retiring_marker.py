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


def test_racing_retire_and_resume_leave_the_record_consistent():
    """Publish and withdraw run under the retirement lock: whatever the interleaving, the record exists exactly
    while some retirement reason is left."""
    import threading

    me = os.getpid()
    barrier = threading.Barrier(8)

    def churn(n):
        barrier.wait()
        for i in range(60):
            key = f"k{n}"
            if i % 2 == 0:
                hll.retire_host_loops(key, f"r{n}")
            else:
                hll.resume_host_loops(key)

    threads = [threading.Thread(target=churn, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert (hll.retiring_reason(me) is not None) == (hll.host_loops_retired() is not None)
    for n in range(8):
        hll.resume_host_loops(f"k{n}")
    assert hll.host_loops_retired() is None and hll.retiring_reason(me) is None


def test_the_record_is_withdrawn_when_the_process_exits():
    hll.retire_host_loops("code-skew", "boot behind")
    assert hll._EXIT_HOOK  # atexit.register(_withdraw_retiring) is in place
    hll._withdraw_retiring()  # what the exit hook runs
    assert hll.retiring_reason(os.getpid()) is None
