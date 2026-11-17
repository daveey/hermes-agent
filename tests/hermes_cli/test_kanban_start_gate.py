"""The plugin start gate: a ``kanban_start_gate`` callback can keep a card ready and unclaimed for one tick. [daveey]"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_start_gate as gate
from hermes_cli import lifecycle
from hermes_cli import plugins
from tests.hermes_cli import test_kanban_llm_pacing as pacing
from tests.hermes_cli import test_kanban_model_tier as model_tier
from tests.hermes_cli.test_kanban_llm_pacing import _dispatch, _opus_card
from tests.hermes_cli.test_kanban_model_tier import _write_cfg

board = model_tier.board  # one unsynced gateway: every card is its own to lease
paced_board = pacing.paced_board  # that board with the governor's caches cleared


def _hook(monkeypatch, answers, seen=None):
    """Subscribe one fake plugin: ``answers`` is a list of returns, or a callable taking the hook kwargs."""
    def invoke(name, **kwargs):
        assert name == gate.HOOK
        if seen is not None:
            seen.append(kwargs)
        return answers(**kwargs) if callable(answers) else list(answers)
    monkeypatch.setattr(lifecycle, "has_hook", lambda name: name == gate.HOOK)
    monkeypatch.setattr(lifecycle, "invoke_hook", invoke)


ASK = dict(task_id="t_1", lane="ready", assignee="default", board=None)


def test_no_subscriber_starts_the_card(monkeypatch):
    monkeypatch.setattr(lifecycle, "has_hook", lambda name: False)
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda *a, **k: pytest.fail("not asked without a subscriber"))
    assert gate.hold(**ASK) is None


def test_a_hold_answer_names_its_reason(monkeypatch):
    seen = []
    _hook(monkeypatch, [None, {"hold": True, "reason": "token_throttle"}], seen)
    assert gate.hold(**ASK) == "start_gate:token_throttle"
    assert seen == [ASK]  # the callback gets the card, its lane, assignee and board
    _hook(monkeypatch, [{"hold": True, "reason": "Token Throttle: over the cap!!"}])
    assert gate.hold(**ASK) == "start_gate:token_throttle_over_the_cap"  # one lowercase word, bounded
    _hook(monkeypatch, [{"hold": True}])
    assert gate.hold(**ASK) == "start_gate:plugin"
    _hook(monkeypatch, [{"hold": True, "reason": "x" * 200}])
    assert gate.hold(**ASK) == "start_gate:" + "x" * 40


@pytest.mark.parametrize("answers", [[], [None], [False], ["hold"], [{"hold": False}], [{"hold": 1}],
                                     [{"hold": "true"}], [{"reason": "token_throttle"}], [[{"hold": True}]]])
def test_anything_but_an_exact_hold_starts_the_card(monkeypatch, answers):
    _hook(monkeypatch, answers)
    assert gate.hold(**ASK) is None


@pytest.mark.parametrize("fault", [RuntimeError("plugin fault"), ImportError("discovery"), AssertionError("x"),
                                   KeyError("k")])
def test_a_failing_hook_or_discovery_starts_the_card_and_warns_once(monkeypatch, caplog, fault):
    def boom(**kwargs):
        raise fault
    _hook(monkeypatch, boom)
    monkeypatch.setattr(gate, "_warned", False)
    with caplog.at_level("WARNING", logger=gate.logger.name):
        assert gate.hold(**ASK) is None and gate.hold(**ASK) is None and gate.hold(**ASK) is None
    assert len([r for r in caplog.records if "start gate" in r.getMessage()]) == 1  # not once per card
    monkeypatch.setattr(lifecycle, "has_hook", lambda name: (_ for _ in ()).throw(ImportError("plugin tree")))
    assert gate.hold(**ASK) is None  # a discovery fault before any callback ran


def test_the_real_hook_dispatch_swallows_a_raising_plugin_callback(monkeypatch):
    """Not through a patched ``invoke_hook``: core's own dispatch catches the callback's exception."""
    manager = plugins._delivery_manager()

    def raising(**kwargs):
        raise RuntimeError("plugin fault")

    def holding(**kwargs):
        return {"hold": True, "reason": "token_throttle"}
    monkeypatch.setattr(manager, "_hooks", {**getattr(manager, "_hooks", {}), gate.HOOK: [raising, holding]})
    if not lifecycle.has_hook(gate.HOOK):
        pytest.skip("this plugin manager keeps its callbacks elsewhere; the patched-hook tests cover the gate")
    assert gate.hold(**ASK) == "start_gate:token_throttle"  # the raising callback is skipped, the next one answers


def test_word_reads_only_start_gate_reasons():
    assert gate.word("start_gate:token_throttle") == "token_throttle"
    for other in ("start_gate:", "start_gate", "active_pr", "llm_pacing", "", None, 7):
        assert gate.word(other) is None


def test_the_hook_is_a_registered_hook_name():
    assert gate.HOOK in plugins.VALID_HOOKS


def test_dispatch_keeps_a_gated_card_ready_and_starts_it_once_the_gate_opens(paced_board, monkeypatch):
    _write_cfg(paced_board)
    holding = {"on": True}
    _hook(monkeypatch, lambda **kw: [{"hold": True, "reason": "token_throttle"}] if holding["on"] else [None])
    with kbc.connect() as conn:
        a, b = _opus_card(conn, "a"), _opus_card(conn, "b")
        seen, result = _dispatch(conn)
        assert seen == [] and kb.get_task(conn, a).status == "ready" and kb.get_task(conn, b).status == "ready"
        assert sorted(result.respawn_guarded) == sorted([(a, "start_gate:token_throttle"), (b, "start_gate:token_throttle")])
        holding["on"] = False
        seen, result = _dispatch(conn)
        assert sorted(seen) == sorted([a, b]) and not result.respawn_guarded  # nothing persisted: it just starts


def test_dispatch_dry_run_reports_the_gate_without_claiming(paced_board, monkeypatch):
    _write_cfg(paced_board)
    _hook(monkeypatch, [{"hold": True, "reason": "token_throttle"}])
    from hermes_cli import kanban_db_dispatch as kdd
    with kbc.connect() as conn:
        tid = _opus_card(conn)
        result = kdd.dispatch_once(conn, dry_run=True)
        assert (tid, "start_gate:token_throttle") in result.respawn_guarded and kb.get_task(conn, tid).status == "ready"


def test_a_gated_card_gets_a_skip_record_and_no_guard_hold_bookkeeping(paced_board, monkeypatch):
    from hermes_cli import kanban_dispatch_skip as skips
    _write_cfg(paced_board)
    _hook(monkeypatch, [{"hold": True, "reason": "token_throttle"}])
    with kbc.connect() as conn:
        tid = _opus_card(conn)
        for _ in range(3):
            seen, _result = _dispatch(conn)
            assert seen == []
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        skip = skips.latest(conn, tid)
        task = kb.get_task(conn, tid)
    assert skip["reason"] == "start_gate" and skip["detail"] == "token_throttle"
    assert skips.words(skip) == "held: a plugin start gate holds card starts (token_throttle)"
    assert kinds.count(skips.EVENT) == 1  # one record for the episode, not one per tick
    assert "respawn_guarded" not in kinds and "blocked" not in kinds and task.status == "ready"  # not a guard hold


def test_tick_skips_lists_only_start_gate_guards():
    from types import SimpleNamespace
    from hermes_cli import kanban_dispatch_skip as skips
    result = SimpleNamespace(respawn_guarded=[("t_1", "start_gate:token_throttle"), ("t_2", "active_pr"),
                                              ("t_3", "llm_pacing")])
    assert skips._tick_skips(result) == [("t_1", "start_gate", "token_throttle", None, {})]


def test_a_fully_gated_queue_is_an_expected_hold_not_a_stuck_dispatcher(paced_board, monkeypatch):
    import time
    from hermes_cli import kanban_dispatch_expected_holds as expected
    _write_cfg(paced_board)
    _hook(monkeypatch, [{"hold": True, "reason": "token_throttle"}])
    with kbc.connect() as conn:
        a, b = _opus_card(conn, "a"), _opus_card(conn, "b")
        _, result = _dispatch(conn)
    holds, covered = expected.expected_holds([("default", result)], time.time())
    assert covered is True and set(holds) == {("default", a), ("default", b)}
    assert all("start_gate:token_throttle" in detail and until is None for detail, until in holds.values())
    result.respawn_guarded.append(("t_other", "active_pr"))  # an ordinary guard hold is still not "expected"
    assert expected.expected_holds([("default", result)], time.time())[1] is False


def test_diagnostics_name_the_gate_instead_of_a_stranded_card(paced_board, monkeypatch):
    from hermes_cli import kanban_diagnostics as kd
    _write_cfg(paced_board)
    _hook(monkeypatch, [{"hold": True, "reason": "token_throttle"}])
    with kbc.connect() as conn:
        tid = _opus_card(conn)
        _dispatch(conn)
        task, events = kb.get_task(conn, tid), kb.list_events(conn, tid)
    assert kd._ready_hold_reason(task, events, {}) == "start_gate"
    with kbc.connect() as conn:
        other = _opus_card(conn, "never asked")
        assert kd._ready_hold_reason(kb.get_task(conn, other), kb.list_events(conn, other), {}) is None


def test_pickup_skips_a_foreign_card_this_machines_gate_would_hold(monkeypatch):
    from hermes_cli import kanban_llm_pacing_dispatch as pd
    from hermes_cli import kanban_worker_node as wn
    monkeypatch.setattr(pd, "pickup_hold", lambda *a, **k: None)
    card = {"id": "t_1", "status": "ready", "assignee": "default"}
    monkeypatch.setattr(lifecycle, "has_hook", lambda name: False)
    assert wn._paced(None, None, card) is None  # no gate: offered as before
    asked = []
    _hook(monkeypatch, [{"hold": True, "reason": "token_throttle"}], asked)
    assert wn._paced(None, None, card) == ("other", "start_gate:token_throttle (local dispatch would hold it)")
    assert asked == [dict(task_id="t_1", lane="ready", assignee="default", board=None)]
    monkeypatch.setattr(pd, "pickup_hold", lambda *a, **k: "anthropic held")
    assert wn._paced(None, None, card) == ("other", "llm_paced: anthropic held (local dispatch would hold it)")
