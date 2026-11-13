"""Regression (2026-11-12, hermes-hub): a sync round longer than the CPU window must not starve the advisory.

The split sampler took snapshot A after one sync round and B after the next, then validated the 1-2 s window. A hub
round takes 3-20 s, so every sample was dropped, ``cache/hub-cpu-sample.json`` aged past 15 s and every hub claim
deferred ``hub_cpu_unknown`` with all slots free. This drives the real ``_run_forever`` loop, the real ``Reporter``
and the real ``machine_cpu_metrics.sample`` window check over real ``/proc/stat``, with the window scaled down
(1 s -> 0.2 s, 1000-2000 ms -> 200-400 ms) so that each sync round outlasts the longest valid window.

Each round waits (bounded) for the advisory to be written twice while it runs. With measurement tied to the loop
nothing is written during a round, the rounds time out and the assertions fail.
"""
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_admission as ka
from hermes_cli import kanban_sync_client as client
from hermes_cli import machine_cpu_metrics as cpu
from hermes_cli import machine_metrics as mm

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/stat")

ROUNDS = 2
RECORDS_PER_ROUND = 2


def test_slow_sync_rounds_keep_the_hub_cpu_advisory_fresh(tmp_path, monkeypatch):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda *a, **k: tmp_path)
    monkeypatch.setattr(cpu, "root_limit", lambda *a, **k: (cpu.ON, 100.0))
    monkeypatch.setattr(cpu, "WINDOW_S", 0.2)
    monkeypatch.setattr(cpu, "MIN_WINDOW_MS", 200)
    monkeypatch.setattr(cpu, "MAX_WINDOW_MS", 400)

    records, recorded, sent = [], threading.Condition(), []
    real_record = mm.record_hub_cpu_sample

    def record(sample, root=None):
        real_record(sample, root)
        with recorded:
            records.append(time.monotonic())
            recorded.notify_all()
    monkeypatch.setattr(mm, "record_hub_cpu_sample", record)

    rounds, advisories, waits = [], [], []

    def slow_round(hub, *, boards):
        t0 = time.monotonic()
        with recorded:
            want = len(records) + RECORDS_PER_ROUND
            recorded.wait_for(lambda: len(records) >= want, timeout=6.0)
        rounds.append((t0, time.monotonic()))
        advisories.append(ka.hub_cpu_advisory())  # what a claim would read right now
        return []

    ident = SimpleNamespace(gateway="test", url="https://example.invalid")
    monkeypatch.setattr(client, "load_identity", lambda: ident)
    monkeypatch.setattr(client, "_schema_ahead_boards", lambda b: [])
    monkeypatch.setattr(client, "board_paths", lambda: [("a", Path("a.db"))])
    monkeypatch.setattr(client, "Hub", lambda i: SimpleNamespace(report_metrics=sent.append))
    monkeypatch.setattr(client, "HubLeaseClient", lambda h: None)
    monkeypatch.setattr(client.sync, "set_lease_client", lambda h: None)
    monkeypatch.setattr(client, "deferred_repairs_due", lambda *a: [])
    monkeypatch.setattr(client, "next_deferred_repair", lambda *a: None)
    monkeypatch.setattr(client, "outbox_pending", lambda *a: False)
    monkeypatch.setattr(client, "reconcile_links", lambda *a, **kw: None)
    monkeypatch.setattr(client, "sync_all_once", slow_round)
    monkeypatch.setattr(client, "FeedWatcher", lambda *a, **kw: SimpleNamespace(run=lambda: None))
    monkeypatch.setattr(client, "wait_for_work", lambda wake, paths, remaining, keep_running:
                        waits.append(remaining) or "period")
    state = SimpleNamespace(clear=lambda: None, record_down=lambda msg: None, started=lambda: None, STOPPED="stopped")

    client._run_forever(lambda: len(rounds) < ROUNDS, 30.0, state)
    # The loop owns the sampler's lifetime: once it returns, the advisory is no longer refreshed (fail-closed).
    assert not [t for t in threading.enumerate() if t.name == "hub-cpu-advisory-sampler"]

    assert len(rounds) == ROUNDS
    assert all(t1 - t0 > cpu.MAX_WINDOW_MS / 1000 for t0, t1 in rounds), rounds  # each round outlasts the window
    assert len(records) >= ROUNDS * RECORDS_PER_ROUND, "advisory not written while the sync round ran"
    for t0, t1 in rounds:  # written during each round, not only between rounds
        assert any(t0 <= at <= t1 for at in records), (t0, t1, records)
    assert "hub_cpu_unknown" not in advisories, advisories  # fresh: hub claims are not deferred as unknown
    sample = mm.read_hub_cpu_sample(tmp_path)
    assert sample is not None and 200 <= sample["cpu_busy_window_ms"] <= 400
    assert any(cpu.FIELDS[0] in s for s in sent)  # the Board report carries a measured triple too
    assert waits and all(w > 20.0 for w in waits), waits  # the sampler adds no loop wake (poll period 30 s)
