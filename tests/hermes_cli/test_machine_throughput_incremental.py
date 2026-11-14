"""Periodic throughput work must depend on changes, not session history size."""
import sqlite3

import pytest

from hermes_cli import machine_throughput_metrics as mt
from hermes_state import SessionDB


@pytest.mark.parametrize("changed", [False, True])
def test_periodic_refresh_vm_work_is_history_bounded(tmp_path, monkeypatch, changed):
    """Instruction counts are deterministic; wall/CPU cadence is a separate benchmark."""
    original_connect = sqlite3.connect
    vm_steps = [0]

    def count_steps():
        vm_steps[0] += 100
        return 0

    def observed_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        conn.set_progress_handler(count_steps, 100)
        return conn

    work = []
    for size in (5000, 12000, 23000):
        root = tmp_path / str(size)
        root.mkdir()
        store = SessionDB(db_path=root / "state.db")
        store.close()
        writer = original_connect(root / "state.db")
        try:
            writer.executemany(
                "INSERT INTO sessions(id,source,started_at) VALUES (?,'test',1)",
                ((f"s{i}",) for i in range(size)),
            )
            writer.executemany(
                "INSERT INTO session_model_usage(session_id,model,input_tokens,output_tokens,"
                "cache_read_tokens,cache_write_tokens) VALUES (?,'m',1,2,3,4)",
                ((f"s{i}",) for i in range(size)),
            )
            writer.commit()
            mt.reset_cache()
            first = mt.cached_counters(root, clock=lambda: 100.0)
            assert first["tokens_io_total"] == 3 * size
            if changed:
                writer.execute("UPDATE session_model_usage SET input_tokens=9 WHERE session_id='s0'")
                writer.commit()
            with monkeypatch.context() as patcher:
                patcher.setattr(mt.sqlite3, "connect", observed_connect)
                vm_steps[0] = 0
                refreshed = mt.cached_counters(root, clock=lambda: 160.0)
                work.append(vm_steps[0])
            assert refreshed["tokens_io_total"] == 3 * size + (8 if changed else 0)
            assert refreshed["tokens_cache_total"] == 7 * size
            assert refreshed["sessions_started_total"] == size
            assert refreshed["sessions_ended_total"] == 0
        finally:
            writer.close()
            mt.reset_cache()
    assert work[-1] <= 1.5 * max(work[0], 100), work
