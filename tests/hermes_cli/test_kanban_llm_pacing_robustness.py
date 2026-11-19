"""The capacity model never changes what holds a start, whatever the inputs or the stored file. [daveey]

Review of 67cbb5a: a year-9999 reset made the curve loop for minutes on the claim path, and a malformed stored
``v2`` made every refresh raise before the save and the cache, so a held pool started cards and the usage
endpoint was read on every call. Labels are synthetic."""
from __future__ import annotations

import json
import math
import sqlite3
import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_llm_pacing as lp
from hermes_cli import kanban_llm_pacing_dispatch as pd
from hermes_cli import kanban_llm_pacing_history as hist
from tests.hermes_cli import test_kanban_stuck_clock_holds as helpers

home = helpers.home
NOW = 1_800_000_000.0
DAY = 86400.0
CFG = lp.PacingConfig(providers=("anthropic",))
TPP = lp.Tpp(2.6e5, lp.MEASURED, NOW)
HOSTILE_RESETS = [253402300799.0, NOW + 127 * 365 * DAY, -1e18, -5.0, math.nan, math.inf, -math.inf, 10 ** 30]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(pd, "_CACHE", {})
    monkeypatch.setattr(pd, "_WARNED", {})
    monkeypatch.setattr(pd, "_MODEL_SCOPED", {})
    monkeypatch.setattr(pd, "_V2_MEMO", {})
    monkeypatch.setattr(hist, "fleet_tokens", lambda provider: None)


# --- 1. hostile resets: bounded cost, bounded curve, serializable -------------------------------------------------

@pytest.mark.parametrize("reset", HOSTILE_RESETS, ids=str)
def test_a_hostile_reset_plans_fast_and_bounded(reset):
    readings = [lp.Reading(NOW, f"h{i}", kind, 40.0, reset if i % 2 else NOW + 3 * DAY)
                for i in range(50) for kind in (lp.SEVEN_DAY, lp.FIVE_HOUR)]
    started = time.perf_counter()
    pool, curve = lp.plan_pool("anthropic", lp.valid_readings(readings), TPP, TPP, NOW, CFG)
    elapsed = time.perf_counter() - started
    print(f"plan_pool 50 accounts, reset={reset!r}: {elapsed * 1000:.2f} ms, {len(curve)} curve points")
    assert elapsed < 0.05
    assert len(curve) <= lp.MAX_CURVE_POINTS
    top = NOW + lp.WINDOW_SECONDS[lp.SEVEN_DAY] + lp.RESET_SLACK_SECONDS
    assert all(NOW <= t <= top for t, *_ in curve)
    assert all(a["reset_at"] is None or NOW <= a["reset_at"] <= top for a in pool["accounts"])
    json.dumps(pool, allow_nan=False)
    json.dumps(curve, allow_nan=False)


def test_a_non_finite_used_reads_as_exhausted_and_never_reaches_json():
    readings = [lp.Reading(NOW, "a", lp.SEVEN_DAY, math.nan, NOW + DAY),
                lp.Reading(NOW, "b", lp.SEVEN_DAY, math.inf, NOW + DAY),
                lp.Reading(NOW, "c", lp.SEVEN_DAY, 90.0, NOW + DAY)]
    assert [(r.account, r.used) for r in lp.valid_readings(readings)] == [("a", 100.0), ("b", 100.0), ("c", 90.0)]
    pool, _ = lp.plan_pool("anthropic", readings, TPP, TPP, NOW, CFG)
    assert pool["at_reserve"] is True and pool["remaining_tokens"] == 0.0
    json.dumps(pool, allow_nan=False)


@pytest.mark.parametrize("pools", [True, [1], "x", 5, None, {"anthropic": 7}, {"anthropic": {"ratio": math.nan}}],
                         ids=repr)
def test_route_ratios_reads_a_malformed_document_as_no_ratios(pools):
    assert lp.route_ratios({"pools": pools}) == {}
    assert lp.route_ratios(pools) == {}


# --- 2. a malformed stored file: held stays held, one usage read per interval ---------------------------------------

def _governed(monkeypatch, clock):
    monkeypatch.setattr(pd, "load_config", lambda: CFG)
    monkeypatch.setattr(pd, "_governed", lambda conn, task_id, lane, cfg: (SimpleNamespace(id=task_id), ["anthropic"]))
    monkeypatch.setattr(pd, "_pace_alternate", lambda *a, **k: None)
    monkeypatch.setattr(pd, "time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]))
    reads = []

    def read(provider, now):
        reads.append(now)
        return [lp.Reading(now, "acct-past-reserve@example.test", lp.SEVEN_DAY, 90.0, now + 2 * DAY)]
    monkeypatch.setattr(pd, "read_pool", read)
    return reads


def _hold():
    return pd.hold(None, {"id": "t_1"}, "ready", SimpleNamespace(respawn_guarded=[], clock_held={}, llm_paced=[]),
                   dry_run=True)


JUNK = [True, [1], "x", 5, None, {"anthropic": 7}]


@pytest.mark.parametrize("field", ["v2", "pools", "curve", "events", "pool"])
@pytest.mark.parametrize("junk", JUNK, ids=repr)
def test_a_malformed_v2_never_disables_the_hold_or_the_read_interval(home, monkeypatch, field, junk):
    clock = [NOW]
    reads = _governed(monkeypatch, clock)
    v2 = {"computed_at": NOW - 10, "pools": {}, "curve": {}, "events": []}
    if field == "v2":
        v2 = junk
    elif field == "pool":
        v2["pools"] = {"anthropic": junk}
    else:
        v2[field] = junk
    pd.state_path().write_text(json.dumps({"version": 2, "v2": v2}), encoding="utf-8")
    for offset in (0, 1, CFG.read_interval - 1):
        clock[0] = NOW + offset
        assert _hold() is True
    assert reads == [NOW]  # one usage read in the interval, then the cache and the rewritten file serve
    stored = json.loads(pd.state_path().read_text())
    assert stored["v2"]["pools"]["anthropic"]["at_reserve"] is True
    clock[0] = NOW + CFG.read_interval
    assert _hold() is True and reads == [NOW, NOW + CFG.read_interval]


@pytest.mark.parametrize("junk", JUNK, ids=repr)
def test_malformed_fields_of_a_fresh_stored_pool_still_hold(home, monkeypatch, junk):
    clock = [NOW]
    reads = _governed(monkeypatch, clock)
    pool = {"read_at": NOW, "at_reserve": True, "until": junk, "accounts": junk, "rate_tokens_per_s": junk,
            "tpp_7d": junk, "tpp_source": junk}
    legacy = {"read_at": NOW, "pace": lp.PoolPace("anthropic", lp.HELD, 0.0).as_dict(), "samples": junk}
    pd.state_path().write_text(json.dumps({"anthropic": legacy, "v2": {"pools": {"anthropic": pool}}}),
                               encoding="utf-8")
    assert _hold() is True and reads == []  # read just now by another dispatcher: no usage read


# --- 3. dispatch-level equivalence: the hold is exactly the legacy ``held`` verdict ---------------------------------

CASES = {
    "nan beside one past reserve": [("a", lp.SEVEN_DAY, math.nan, 2), ("b", lp.SEVEN_DAY, 90.0, 2)],
    "sole account inf": [("a", lp.SEVEN_DAY, math.inf, 2)],
    "nan beside one under": [("a", lp.SEVEN_DAY, math.nan, 2), ("b", lp.SEVEN_DAY, 40.0, 2)],
    "one past reserve": [("a", lp.SEVEN_DAY, 90.0, 2)],
    "one under": [("a", lp.SEVEN_DAY, 40.0, 2)],
    "mixed": [("a", lp.SEVEN_DAY, 100.0, 1), ("b", lp.SEVEN_DAY, 50.0, 3)],
    "5h reserved": [("a", lp.SEVEN_DAY, 10.0, 3), ("a", lp.FIVE_HOUR, 81.0, 0.1)],
    "all reserved": [("a", lp.SEVEN_DAY, 86.0, 1), ("b", lp.SEVEN_DAY, 20.0, 2), ("b", lp.FIVE_HOUR, 95.0, 0.1)],
    "none read": [],
}


def _break_v2_plan(monkeypatch):
    monkeypatch.setattr(lp, "plan_pool", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("plan broke")))


def _break_v2_update(monkeypatch):
    monkeypatch.setattr(hist, "update_v2", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("update broke")))


@pytest.mark.parametrize("mode", ["file", "memo", "junk-file", "plan-raises", "update-raises", "history-locked"])
@pytest.mark.parametrize("case", sorted(CASES))
def test_the_dispatch_hold_is_the_legacy_held_verdict(home, monkeypatch, case, mode):
    clock = [NOW]
    _governed(monkeypatch, clock)
    windows = CASES[case]
    readings = [lp.Reading(NOW, f"{a}@example.test", kind, used, NOW + days * DAY) for a, kind, used, days in windows]
    monkeypatch.setattr(pd, "read_pool", lambda provider, now: list(readings))
    # The legacy verdict on the same readings, a non-finite ``used`` read as exhausted (``valid_readings``).
    legacy = lp.pool_pace("anthropic", lp.valid_readings(readings), [], CFG).state == lp.HELD
    expected = {"nan beside one past reserve": True, "sole account inf": True, "nan beside one under": False}
    assert legacy is expected.get(case, legacy)
    setup = {
        "memo": lambda: monkeypatch.setattr(pd, "_save_state", lambda *a: None),
        "junk-file": lambda: pd.state_path().write_text('{"anthropic": 3, "v2": [1], "version": "x"}',
                                                        encoding="utf-8"),
        "plan-raises": lambda: _break_v2_plan(monkeypatch),
        "update-raises": lambda: _break_v2_update(monkeypatch),
    }
    setup.get(mode, lambda: None)()
    lock = None
    if mode == "history-locked":
        lock = hist.connect(pd.history_path())
        lock.execute("BEGIN EXCLUSIVE")
    try:
        assert _hold() is legacy
    finally:
        if lock is not None:
            lock.execute("ROLLBACK")
            lock.close()


# --- 4. a busy history store never delays a claim beyond its lock timeout -------------------------------------------

def test_a_locked_history_store_is_skipped_quickly_and_the_hold_stands(home, monkeypatch):
    clock = [NOW]
    reads = _governed(monkeypatch, clock)
    lock = sqlite3.connect(str(pd.history_path()))
    hist.connect(pd.history_path()).close()
    lock.execute("BEGIN EXCLUSIVE")
    try:
        started = time.perf_counter()
        held = _hold()
        elapsed = time.perf_counter() - started
    finally:
        lock.execute("ROLLBACK")
        lock.close()
    print(f"hold() with the history store locked: {elapsed * 1000:.0f} ms")
    assert held is True and reads == [NOW]
    assert elapsed < hist.LOCK_TIMEOUT_SECONDS + 0.5
    assert json.loads(pd.state_path().read_text())["v2"]["pools"]["anthropic"]["at_reserve"] is True
