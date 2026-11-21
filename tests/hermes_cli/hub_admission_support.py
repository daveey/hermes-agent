"""Shared board and claim helpers for the hub-admission suites. [daveey]

``test_kanban_hub_admission``, its ``test_kanban_hub_admission_*`` siblings, ``test_kanban_hub_gate_convoy`` and
``test_hub_cpu_gate_contention`` build a throwaway board, write the admission config and drive claims through the CPU
gate; they import these helpers from here so no test module imports another. Each module that needs a board declares
its own ``board`` fixture over :func:`open_board`.
"""
from __future__ import annotations

import time

from hermes_cli import kanban_admission as ka
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import machine_metrics as mm


def open_board(tmp_path, monkeypatch):
    """Generator body for a ``board`` fixture: ``(conn, home)`` on a fresh board with HERMES_HOME pointed at it."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn, tmp_path
    finally:
        conn.close()


def configure(home, limit=50, cap=None):
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    lines = [_managed_yaml().rstrip(), "kanban:"]
    if cap is not None:
        lines.append(f"  max_in_progress: {cap}")
    if limit is not None:
        lines += ["  admission:", f"    cpu_busy_below_pct: {limit}"]
    (home / "config.yaml").write_text("\n".join(lines) + "\n")


def advisory(home, pct=10.0, age_ms=0):
    mm.record_hub_cpu_sample({"cpu_busy_pct": pct, "cpu_busy_window_ms": 1000,
                              "cpu_busy_sample_end_ms": int(time.time() * 1000) - age_ms}, root=home)


def task(conn, review=False, title="gate"):
    from tests.hermes_cli.kanban_review_fixture import SOURCE_TITLE, create_review
    if review and conn.execute("SELECT 1 FROM tasks WHERE title=? AND status='done'", (SOURCE_TITLE,)).fetchone():
        return create_review(conn, title, "default")
    t = kb.create_task(conn, title=title, assignee="default")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status=? WHERE id=?", ("review" if review else "ready", t))
    return t


def claim(conn, t, review):
    return (kb.claim_review_task if review else kb.claim_task)(conn, t)


def fake_sample(pct, calls=None, check=None):
    def sample(*a, **k):
        if check:
            check()
        if calls is not None:
            calls.append(time.monotonic())
        if pct is None:
            return None
        return {"cpu_busy_pct": pct, "cpu_busy_window_ms": 1000, "cpu_busy_sample_end_ms": int(time.time() * 1000)}
    return sample


def runs(conn):
    return conn.execute("SELECT count(*) FROM task_runs").fetchone()[0]


def refused(conn, t, review, reason):
    assert kb.get_task(conn, t).status == ("review" if review else "ready")
    assert runs(conn) == 0
    assert sync.pop_claim_deferral(t) == reason


def locked(monkeypatch, read):
    """Feed the final gate's locked read of the sampler sample from ``read()`` (a ``fake_sample`` or a ``pytest.fail``
    stand-in). The pre-lease advisory (stricter age) still reads the real file written with :func:`advisory`."""
    real = ka.sampler_sample

    def sampler_sample(max_age_ms=None):
        return read() if max_age_ms is None else real(max_age_ms)
    monkeypatch.setattr(ka, "sampler_sample", sampler_sample)
