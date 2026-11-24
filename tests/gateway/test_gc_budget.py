"""Gateway and serve processes collect garbage less often and time every collection. [daveey]"""
from __future__ import annotations

import gc

import pytest

from gateway import gc_budget


@pytest.fixture
def restore_gc():
    before = gc.get_threshold()
    callbacks = list(gc.callbacks)
    totals = {k: list(v) for k, v in gc_budget._totals.items()}
    yield before
    gc.set_threshold(*before)
    gc.callbacks[:] = callbacks
    gc_budget._totals.clear()
    gc_budget._totals.update(totals)


@pytest.mark.parametrize(("raw", "expected"), [
    (None, gc_budget.GATEWAY_GC_THRESHOLD0), ("", gc_budget.GATEWAY_GC_THRESHOLD0),
    ("50000", 50000), ("7000.9", 7000), ("0", None), ("-5", None), ("many", None), ("inf", None)])
def test_threshold_from_environment(raw, expected):
    environ = {} if raw is None else {gc_budget.ENV: raw}
    assert gc_budget.gateway_gc_threshold0(environ) == expected


def test_apply_raises_only_the_young_threshold(restore_gc):
    gc.set_threshold(2000, 10, 10)
    assert gc_budget.apply_gateway_gc_budget({}) == (gc_budget.GATEWAY_GC_THRESHOLD0, 10, 10)
    assert gc_budget.apply_gateway_gc_budget({gc_budget.ENV: "0"}) == (gc_budget.GATEWAY_GC_THRESHOLD0, 10, 10)
    gc.set_threshold(2000, 10, 10)
    assert gc_budget.apply_gateway_gc_budget({gc_budget.ENV: "0"}) == (2000, 10, 10)  # opt-out keeps the default


def test_apply_installs_one_timer_and_times_collections(restore_gc):
    gc_budget.apply_gateway_gc_budget({})
    gc_budget.apply_gateway_gc_budget({})
    assert gc.callbacks.count(gc_budget._on_gc) == 1
    gc_budget._totals.clear()
    cycle = []
    cycle.append(cycle)
    del cycle
    gc.collect()
    gc.collect(0)
    snap = gc_budget.snapshot()
    assert snap["timed"] is True and snap["threshold"][0] == gc_budget.GATEWAY_GC_THRESHOLD0
    full, young = snap["generations"]["2"], snap["generations"]["0"]
    assert full["runs"] == 1 and young["runs"] == 1
    assert full["collected"] >= 1
    assert full["wall_s"] >= full["max_wall_s"] > 0
    assert full["cpu_s"] >= 0


def test_timer_survives_a_collection_started_while_the_accounting_lock_is_held(restore_gc):
    from hermes_cli import loop_accounting

    gc_budget.apply_gateway_gc_budget({})
    with loop_accounting._lock:  # a collection can begin inside any allocation, this one included
        gc.collect()
    assert gc_budget.snapshot()["generations"]["2"]["runs"] >= 1


def test_accounting_snapshot_reports_gc(restore_gc):
    from hermes_cli import loop_accounting

    gc_budget.apply_gateway_gc_budget({})
    gc.collect()
    snap = loop_accounting.snapshot()
    assert snap["gc"]["generations"]["2"]["runs"] >= 1
    assert snap["gc"]["threshold"][0] == gc_budget.GATEWAY_GC_THRESHOLD0
