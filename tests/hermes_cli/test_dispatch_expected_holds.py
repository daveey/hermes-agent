"""Intentional admission holds are not a broken dispatcher. [daveey]"""
import argparse

import pytest

from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_ops


def paced(*, until=2000, limit=0, running=None):
    """A pool at reserve (``held``) until ``until``; ``until=None`` is a hold nothing frees (stays unexplained)."""
    result = kbd.DispatchResult(lane_candidates=2)
    result.skipped_ineligible.append(("t_elsewhere", "labels_mismatch"))
    result.respawn_guarded.append(("t_wait", "llm_pacing"))
    result.clock_held["t_wait"] = ("llm_pacing", until or 2000)
    result.llm_paced.append(("t_wait", "openai-codex paced", until, {"providers": [
        {"provider": "openai-codex", "state": "held", "limit": limit,
         **({} if running is None else {"running": running})}]}))
    return result


def daemon(monkeypatch, capsys, result):
    monkeypatch.setattr(kanban_ops.kbc, "connect_closing", lambda: None)
    # Avoid database reads: the observed ready queue is deliberately nonempty.
    result.skipped_unassigned = []
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(kanban_ops.kbc, "connect_closing", Connection)
    monkeypatch.setattr(kbd, "has_spawnable_ready", lambda conn: True)
    def run(*, on_tick, **kwargs):
        for _ in range(8):
            on_tick(result)
    monkeypatch.setattr(kbd, "run_daemon", run)
    kanban_ops._cmd_daemon(argparse.Namespace(force=True, interval=1, max=None, pidfile=None))
    return capsys.readouterr().err


# "full" (a paced pool at this host's start limit) is gone with the start limit: only a held pool holds a start.
@pytest.mark.parametrize("kind", ["timed", "pressure"])
def test_daemon_expected_hold_not_stuck(monkeypatch, capsys, kind):
    monkeypatch.setattr(kanban_ops.time, "time", lambda: 1000)
    result = paced(until=1600 if kind == "timed" else None)
    if kind == "pressure":
        result = kbd.DispatchResult(lane_candidates=1, claim_deferred=[("t_wait", "worker_load_pressure")])
    output = daemon(monkeypatch, capsys, result)
    assert "kanban dispatcher stuck" not in output
    assert "kanban dispatch held" in output
    assert ("worker_load_pressure" if kind == "pressure" else "llm_pacing") in output


def test_long_deferral_warns_despite_unrelated_spawns():
    from hermes_cli.kanban_dispatch_expected_holds import ExpectedHoldMonitor
    monitor = ExpectedHoldMonitor()
    messages = []
    result = paced(until=1100)  # held until a reset at 1100: reported 15 min after it
    emit = lambda level, text: messages.append((level, text))
    assert monitor.observe([("a", result)], 1000, emit)
    result.spawned.append(("t_other", "default", ""))
    result.llm_paced[0] = (*result.llm_paced[0][:2], 1e9, result.llm_paced[0][3])  # a rolling refresh
    monitor.observe([("a", result)], 2000, emit)
    assert any(level == 30 and "t_wait" in text and "1000s" in text for level, text in messages)
    assert all("dispatcher stuck" not in text for _, text in messages)


@pytest.mark.parametrize("until,limit,running", [(None, 0, 0), (999, 0, 0)])
def test_invalid_pacing_clock_does_not_hide_starvation(monkeypatch, capsys, until, limit, running):
    monkeypatch.setattr(kanban_ops.time, "time", lambda: 1000)
    result = paced(until=until, limit=limit, running=running)
    result.skipped_ineligible.clear()
    result.lane_candidates = 1
    assert "kanban dispatcher stuck" in daemon(monkeypatch, capsys, result)


def test_deadline_is_real_reset_not_refresh():
    from hermes_cli.kanban_dispatch_expected_holds import ExpectedHoldMonitor
    monitor = ExpectedHoldMonitor()
    messages = []
    emit = lambda level, text: messages.append((level, text))
    monitor.observe([("a", paced(until=4000, limit=0, running=0))], 1000, emit)
    monitor.observe([("a", paced(until=4000, limit=0, running=0))], 1900, emit)
    assert not any(level == 30 for level, _ in messages)
    # Repeatedly extending an advertised reset must not erase the original wait.
    monitor.observe([("a", paced(until=8000, limit=0, running=0))], 4900, emit)
    assert any(level == 30 for level, _ in messages)


def test_unknown_claim_reason_is_not_hidden(monkeypatch, capsys):
    monkeypatch.setattr(kanban_ops.time, "time", lambda: 1000)
    result = paced()
    result.claim_deferred.append(("t_broken", "offline"))
    result.lane_candidates += 1
    assert "kanban dispatcher stuck" in daemon(monkeypatch, capsys, result)


@pytest.mark.parametrize("kind", ["timed", "pressure"])
def test_gateway_expected_hold_not_stuck(monkeypatch, caplog, tmp_path, kind):
    import logging
    from gateway import kanban_watchers as watchers
    from tests.hermes_cli.test_kanban_stuck_clock_holds import _run_gateway_ticks
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setattr(watchers.time, "time", lambda: 1000)
    result = paced(until=1600 if kind == "timed" else None)
    if kind == "pressure":
        result = kbd.DispatchResult(lane_candidates=1, claim_deferred=[("t_wait", "worker_load_pressure")])
    monkeypatch.setattr(watchers._KanbanDispatcher, "tick_once", lambda self, **kw: [("default", result)])
    monkeypatch.setattr(watchers._KanbanDispatcher, "ready_nonempty", lambda self: True)
    caplog.set_level(logging.INFO, logger="gateway")
    _run_gateway_ticks(monkeypatch, watchers._HEALTH_WINDOW + 4)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "kanban dispatcher stuck" not in text
    assert "kanban dispatch held" in text


@pytest.mark.parametrize("split", [False, True])
def test_mixed_clock_and_pacing_are_expected(split):
    from hermes_cli.kanban_dispatch_expected_holds import expected_holds
    result = paced()
    clock = kbd.DispatchResult(lane_candidates=1)
    clock.clock_held["t_clock"] = ("subscription_quota_reset", 4000)
    clock.respawn_guarded.append(("t_clock", "subscription_quota_reset"))
    if split:
        rows = [("a", result), ("b", clock)]
    else:
        result.clock_held.update(clock.clock_held)
        result.respawn_guarded += clock.respawn_guarded
        result.lane_candidates += 1
        rows = [("a", result)]
    assert expected_holds(rows, 1000)[1]
    result.admission_deferred.append("t_unknown")
    assert not expected_holds(rows, 1000)[1]


@pytest.mark.parametrize("value", [None, {"providers": [None]}, {"providers": []}])
def test_malformed_pacing_is_unexplained_not_exception(value):
    from hermes_cli.kanban_dispatch_expected_holds import expected_holds
    result = paced()
    result.llm_paced = [("t_wait", "detail", None, value)]
    assert not expected_holds([("a", result)], 1000)[1]


def test_pressure_warning_is_bounded_and_resets_after_card_leaves():
    from hermes_cli.kanban_dispatch_expected_holds import ExpectedHoldMonitor
    monitor = ExpectedHoldMonitor()
    result = kbd.DispatchResult(lane_candidates=1, claim_deferred=[("t_wait", "worker_load_pressure")])
    messages = []
    emit = lambda level, text: messages.append((level, text))
    for now in (1000, 1899):
        assert monitor.observe([("a", result)], now, emit)
    assert not any(level == 30 for level, _ in messages)
    for now in (1900, 1901, 2199, 2200):
        monitor.observe([("a", result)], now, emit)
    assert sum(level == 30 for level, _ in messages) == 2
    monitor.observe([("a", kbd.DispatchResult())], 2201, emit)
    messages.clear()
    monitor.observe([("a", result)], 3000, emit)
    assert not any(level == 30 for level, _ in messages)


@pytest.mark.parametrize("field,value", [("admission_deferred", ["t_x"]), ("memory_pressure", "critical"),
    ("ownership_error", "broken"), ("quiet_deferred", [("t_x", "episode")]), ("reclaimed", ["t_x"])])
def test_other_outcome_never_masked(field, value):
    from hermes_cli.kanban_dispatch_expected_holds import expected_holds
    result = paced()
    setattr(result, field, value)
    assert not expected_holds([("a", result)], 1000)[1]


def test_real_pacing_record_is_classified_without_writing(monkeypatch):
    from hermes_cli import kanban_llm_pacing as lp
    from hermes_cli import kanban_llm_pacing_dispatch as dispatch
    from hermes_cli.kanban_dispatch_expected_holds import expected_holds
    monkeypatch.setattr(dispatch.time, "time", lambda: 1000)
    result = kbd.DispatchResult(lane_candidates=2, skipped_ineligible=[("t_elsewhere", "labels_mismatch")])
    dispatch._record(None, "t_wait", [{"provider": "openai-codex", "state": "held", "until": 4000.0, "limit": 0,
                     "detail": "2 account(s) all at reserve"}], result, dry_run=True, cfg=lp.PacingConfig())
    holds, covered = expected_holds([("a", result)], 1000)
    assert covered
    assert "openai-codex held" in holds[("a", "t_wait")][0] and "next reset" in holds[("a", "t_wait")][0]
    assert holds[("a", "t_wait")][1] == 4000
