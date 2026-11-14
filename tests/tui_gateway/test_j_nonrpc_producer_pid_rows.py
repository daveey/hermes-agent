"""J complete producer and PID-memo rows over a 600 s window. [daveey]

Card J (t_01a1d862f6e2fc9e), design t_01a1d563324d7e8e §3: producer_admission <= 0.2% of a core, gateway PID
memo <= 0.05%. The component benches in test_producer_admission_gate.py and test_status_pid_memo.py time a few
passes; these replay the whole 600 s window of the measured stream through the real code, once (no best-of).

Producer: 11 profile stores (the hub's count), the real ``sweep_once`` every ``INTERVAL_S`` (40 passes), each
store committing before every pass (state.db's data_version moved in 140 of 300 2 s windows, so every 15 s pass
sees one: the measured stream's worst case). The root store carries the sessions (5k / 23k), checkpointed or
with a real >= 400 MiB WAL held by a long reader. Board tasks and replica rows are not read by the producer (its
only query is ``producer_admissions`` on each state.db), so those dimensions do not apply.

PID memo: 11 profiles listed every 2 s (300 lists) on a virtual clock, so the ``RECHECK_S`` backstop fires as it
does live (every 30 s, each profile reprobed). The probe is the real ``get_running_pid`` against real PID and
lock files whose lock this process holds; only the gateway identity matcher is replaced, because a pytest
process's command line is not a gateway's. This path reads no SQLite store, session, task or replica row, so
none of the scale or WAL dimensions apply. It takes well under a second, so it runs in the default suite; the
producer cells (each pads a 400 MiB WAL or compares growth on millisecond passes) need ``J_ROWS_RUN=1``.
"""
import contextlib
import fcntl
import json
import os
import sqlite3
import time

import pytest

from tests.tui_gateway.test_j_nonrpc_rows import WAL_BYTES, _pad_wal, _production_io, _write_evidence
from tests.tui_gateway.test_producer_admission import _db
from tui_gateway import producer_admission as delivery

SECONDS, PROFILES = 600, 11
PRODUCER_CELLS = {
    "s5k": {"sessions": 5000, "large_wal": False},
    "s23k": {"sessions": 23_000, "large_wal": False},
    "s5k_wal400m": {"sessions": 5000, "large_wal": True},
    "s23k_wal400m": {"sessions": 23_000, "large_wal": True},
}
_PRODUCER: dict = {}


@pytest.fixture(autouse=True)
def _fresh_observers():
    delivery.reset_observers()
    yield
    delivery.reset_observers()


@pytest.mark.skipif(not os.environ.get("J_ROWS_RUN"), reason="J full-row benchmark: set J_ROWS_RUN=1 on a quiet host")
@pytest.mark.parametrize("name", list(PRODUCER_CELLS))
def test_complete_producer_row(name):
    cell = PRODUCER_CELLS[name]
    stores = [_db(None)] + [_db(f"p{n}") for n in range(PROFILES - 1)]
    holders = []
    try:
        for db in stores:
            db.create_session("s0", "desktop")
        root = stores[0].db_path
        with contextlib.closing(sqlite3.connect(root)) as writer:
            writer.executemany("INSERT INTO sessions(id, source, started_at) VALUES (?, 'cli', 1)",
                               [(f"r{n}",) for n in range(cell["sessions"])])
            writer.commit()
        if cell["large_wal"]:
            _pad_wal(root, holders)
        else:
            with contextlib.closing(sqlite3.connect(root)) as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert delivery.sweep_once() == []  # cold pass: opens the observers, not part of the window
        passes = int(SECONDS / delivery.INTERVAL_S)
        samples = []
        with _production_io():
            for i in range(passes):
                for db in stores:
                    db.append_message("s0", "user", f"tick {i}")
                started = time.thread_time()
                assert delivery.sweep_once() == []
                samples.append(time.thread_time() - started)
        wal = os.path.getsize(f"{root}-wal") if os.path.exists(f"{root}-wal") else 0
    finally:
        for db in stores:
            db.close()
        for conn in holders:
            conn.close()
    evidence = {"row": "producer", "cell": cell, "name": name, "passes": passes, "stores": PROFILES,
                "cpu_s": sum(samples), "samples_s": samples, "core": sum(samples) / SECONDS, "root_wal_bytes": wal}
    _write_evidence(evidence)
    _PRODUCER[name] = evidence
    assert passes == 40
    if cell["large_wal"]:
        assert wal >= WAL_BYTES, evidence
    assert evidence["core"] <= 0.002, evidence
    if name != "s5k":
        base = _PRODUCER.get("s5k")
        assert base is not None, "run the whole parametrization: growth compares with the s5k cell"
        assert evidence["core"] / base["core"] <= 1.5, (evidence["core"], base["core"])


def test_complete_pid_memo_row(tmp_path, monkeypatch):
    from gateway import status
    from gateway import status_pid_memo as memo

    memo.clear()
    me = os.getpid()
    monkeypatch.setattr(status, "_record_matches_live_gateway_pid",
                        lambda record, pid, **kwargs: pid == me)  # the identity matcher: not a gateway argv
    record = {"pid": me, "kind": "hermes-gateway", "start_time": status._get_process_start_time(me)}
    paths, held = [], []
    for n in range(PROFILES):
        home = tmp_path / f"p{n}"
        home.mkdir()
        pid_path = home / "gateway.pid"
        pid_path.write_text(json.dumps(dict(record, hermes_home=str(home))))
        lock = status._get_gateway_lock_path(pid_path)
        lock.write_text(json.dumps(dict(record, hermes_home=str(home))))
        handle = open(lock, "r+")  # a real held lock, so the real probe takes its live-gateway branch
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        held.append(handle)
        paths.append(pid_path)
    probes = []
    real_probe = memo._default_probe

    def probe(path):
        probes.append(path)
        return real_probe(path)

    monkeypatch.setattr(memo, "_default_probe", probe)
    try:
        for path in paths:  # the Desktop's first list: every profile probed once, outside the window
            assert memo.running_pid(path, now=0.0) == me
        probes.clear()
        lists = SECONDS // 2
        with _production_io():
            started = time.thread_time()
            for i in range(1, lists + 1):
                for path in paths:
                    assert memo.running_pid(path, now=2.0 * i) == me
            cpu = time.thread_time() - started
    finally:
        for handle in held:
            handle.close()
        memo.clear()
    rechecks = len(probes) // PROFILES
    evidence = {"row": "pid_memo", "lists": lists, "profiles": PROFILES, "probes": len(probes),
                "rechecks_per_profile": rechecks, "cpu_s": cpu, "core": cpu / SECONDS}
    _write_evidence(evidence)
    assert len(probes) % PROFILES == 0 and rechecks == int(SECONDS // memo.RECHECK_S), evidence
    assert evidence["core"] <= 0.0005, evidence
